# Limite de profondeur JSON du shim embedding

## But

Rendre le rejet des corps JSON profondément imbriqués indépendant de la version de CPython.
Le shim doit refuser une profondeur supérieure à 64 avant `json.loads`, avec la réponse existante
`400 {"detail":"Invalid JSON body"}` et sans appel backend ni fuite du corps.

Ticket Brain : `49bda801-d14e-489a-9662-c49c8c6cab59`.

## État prouvé

- Python 3.12.12 rejette le cas historique à 10 000 tableaux par `RecursionError`.
- Python 3.14.0 accepte ce même JSON, puis le validateur métier renvoie une erreur différente.
- `ShimLimits` a un impact GitNexus `LOW` : deux dépendants directs et aucun flux indexé.
- `_read_and_validate` a un impact `LOW` mais partiel, car GitNexus indexe mal cette fonction
  générique imbriquée. La lecture source confirme trois handlers et quatre routes POST.

## Contrat

- `ShimLimits.max_json_depth` vaut 64 par défaut.
- La profondeur est le maximum simultané d'objets `{}` et tableaux `[]` ouverts. Un conteneur
  racine compte pour 1; une racine scalaire compte pour 0.
- Les payloads publics légitimes atteignent au plus 2 (`objet` puis `liste`). La borne 64 conserve
  donc une marge importante tout en restant indépendante de la limite de récursion CPython.
- Un payload métier valide de profondeur 64 est accepté; le même payload à 65 est rejeté sur les
  quatre routes POST avant tout appel à `json.loads`.
- Les délimiteurs dans une chaîne JSON, y compris après échappement, ne comptent pas.
- Le shim préserve les encodages actuellement acceptés par `json.loads(bytearray)` : UTF-8,
  UTF-16, UTF-32 et leurs BOM reconnus. Le scanner travaille sur le même texte décodé.
- Les erreurs de syntaxe, d'encodage, de limite d'entiers et de profondeur partagent la réponse
  bornée existante. Aucun corps ni extrait de corps n'est journalisé.

## Conception minimale

Ajouter un scanner itératif privé dans `services/embedding_shim/shim_app.py`. Le shim décode le
corps avec le détecteur d'encodage utilisé par le module `json` et `errors="surrogatepass"`, puis
parcourt le texte une fois.
Dans une chaîne, `escaped` consomme exactement le caractère suivant avant de redevenir faux;
sinon `\` l'active et `"` ferme la chaîne. Hors chaîne, `"` ouvre une chaîne, `[{` incrémentent et
`]}` décrémentent sans passer sous zéro. Le scanner refuse dès que la profondeur dépasse la limite;
`json.loads` reste l'autorité pour la syntaxe, les types et ses extensions existantes.

Le scanner coûte O(n) en temps et O(1) en mémoire auxiliaire vis-à-vis de la profondeur, avec
sortie anticipée. Le décodage et l'objet JSON restent O(n), bornés par le corps de 8 MiB et la gate
ingress.

La solution n'ajoute aucune dépendance, ne change pas `sys.setrecursionlimit`, ne parcourt pas
récursivement l'objet décodé et ne transforme pas ce chemin d'audit en mécanisme de recovery.

## TDD et commits

1. **RED — contrats**
   - remplacer l'hypothèse CPython à 10 000 niveaux par un corps de profondeur 65;
   - utiliser un payload métier valide par route : exiger `200` et l'appel backend exact à 64,
     puis la réponse `400` exacte, aucun backend et aucune fuite à 65;
   - instrumenter le module local : `json.loads` est appelé zéro fois à 65 et une fois à 64;
   - contractualiser la valeur par défaut et une limite injectée plus petite;
   - caractériser les séries de 1 à 4 antislashs avant un guillemet, `\u005B`, les délimiteurs
     littéraux dans les chaînes, l'UTF-8 invalide et la compatibilité UTF-16/32/BOM;
   - exécuter le test ciblé sous Python 3.12 et 3.14 et conserver l'échec attendu.
2. **GREEN — garde applicative**
   - ajouter `max_json_depth=64`, le scanner itératif et l'appel avant `json.loads`;
   - ne modifier aucun autre comportement métier.
3. **Documentation**
   - ajouter la profondeur 64 aux limites versionnées dans `README.md`, `CLAUDE.md` et
     `docs/ARCHITECTURE.md`;
   - mettre à jour le contrat exact dans `tests/unit/test_documentation_contract.py`;
   - après CI, réconcilier le registre roadmap et le ticket Brain avec les preuves exactes.

## Gates

- test ciblé Python 3.12 puis Python 3.14 dans deux environnements isolés et verrouillés; le RED
  doit être un échec d'assertion attendu, jamais une erreur d'installation ou de collection;
- suite `tests/unit/test_embedding_shim.py` complète sous les deux versions;
- contrat `tests/unit/test_documentation_contract.py` ciblé;
- suite unitaire complète Python 3.12;
- Ruff check et format, `mypy services/embedding_shim/shim_app.py`, compileall avec cache hors
  worktree et `git diff --check`;
- `gitnexus_detect_changes`, revue TDD, revue sécurité/compatibilité et revue finale du diff;
- merge non-force dans `main`, tests post-merge, push GitHub/GitLab et pipeline GitLab verte sur
  le SHA exact.

## Non-goals

- déployer ou redémarrer le shim;
- ajouter l'authentification SEC2-B ou modifier la topologie réseau;
- étendre la limite au profil legacy PyTorch;
- ajouter une nouvelle dépendance ou une matrice CI Python générale.

## Rollback

Le rollback Git restaure le comportement dépendant de CPython. Il n'affecte ni données, ni schéma,
ni runtime tant qu'aucun rollout séparé n'est autorisé.
