# SEC2-A — Frontière réseau et ressources de l'endpoint embedding

Date : 2026-07-24

Branche : `feat/sec2-embedding-hardening`

Base : `main` à `c5fd9e1`

Ticket Brain : `530d796a-42e8-48d9-91a2-5d2a17fdb53b`

## Objectif

Durcir l'état versionné du service embedding canonique : préparer un publish hôte uniquement sur
loopback et refuser avant calcul les corps, lots et concurrences qui dépassent un contrat
explicite. Le runtime reste inchangé et LAN-wide jusqu'à un rollout opérateur séparé. Le
changement doit préserver les enveloppes des consommateurs Brain locaux et Docker connus,
conserver le healthcheck de bout en bout et ne pas présenter l'authentification comme livrée tant
que le client actif `auto-discord` n'a pas reçu le même secret.

## État observé

- `embedding-shim` et le rollback `embedding` publient actuellement `8003:8003`, donc toutes les
  interfaces IPv4/IPv6 de l'hôte.
- Le shim lit `request.json()` sans limite de corps, accepte des lots de taille arbitraire et ne
  borne pas le nombre de calculs simultanés. Seul chaque texte est tronqué à 20 000 caractères
  dans le backend.
- Le serveur llama interne n'est pas publié sur l'hôte et fonctionne avec `-np 1`.
- Les processus Brain actifs utilisent `http://localhost:8003`. Le gateway optionnel utilise le
  DNS Docker `embedding-shim:8003`.
- `auto-discord` appelle activement `brain_v42_embedding_shim:8003` via le réseau externe
  `hawkixs-infra`, sans header d'authentification. Son trafic ne passe pas par le publish hôte.
- Le seul endpoint LAN codé en dur trouvé dans `red-shrik` est `192.168.1.11:8003`; il désigne
  l'ancien service `red-data`/nomic du dev-pc et non le shim Brain canonique sur le PC serveur.
- Les journaux observés sur cinq jours montrent 4 263 appels `/embed` depuis `auto-discord` et
  aucun consommateur LAN distinct identifié. `docker-proxy` masque toutefois l'adresse source
  des éventuels appels via le publish : cette observation n'est pas une preuve d'absence absolue.
- Le digest llama.cpp est déjà verrouillé par OPS1. Le chemin `deploy/dev-pc` est explicitement
  superseded pour le trafic Brain actif.

## Décisions de contrat

1. Les publishes du shim canonique et du rollback legacy deviennent
   `127.0.0.1:8003:8003`. Uvicorn reste sur `0.0.0.0:8003` dans le conteneur afin de servir les
   réseaux Docker.
2. Le corps brut maximal d'une requête de calcul est de **8 MiB**. Cette enveloppe accepte la
   sérialisation JSON réelle des clients `httpx` maintenus pour 100 textes de 20 000 caractères,
   même à quatre octets UTF-8 par caractère; un test construit cette enveloppe maximale au lieu
   de supposer son overhead. La limite est vérifiée sur `Content-Length` lorsqu'il est présent
   puis sur les octets réellement reçus; exactement 8 MiB reste accepté, le premier octet
   supplémentaire produit `413 {"detail":"Request body too large"}`.
3. La lecture complète d'un body doit finir en **5 secondes**. Un flux trop lent produit
   `408 {"detail":"Request body timeout"}`. Au plus **8 bodies** sont lus/validés simultanément
   par worker, ce qui borne la mémoire brute à 64 MiB; une admission supplémentaire reçoit
   `503 {"error":"ingress_busy"}` avec `Retry-After: 1`. Cette réponse distincte n'est jamais
   comptée comme une contention GPU. Le timeout empêche huit slowloris de conserver les slots
   indéfiniment.
4. `/embed` accepte au plus **100 textes**, soit le maximum du CLI de backfill maintenu, et
   `/rerank` au plus **128 candidats**, au-dessus du maximum coalescé observé de 120. Un
   dépassement produit respectivement
   `400 {"detail":"texts must contain at most 100 items"}` ou
   `400 {"detail":"candidates must contain at most 128 items"}`, sans backend. Les lots vides
   et le query-param legacy restent valides. Le CLI non borné `regen_embeddings` est aligné à
   100 dans ce lot.
5. Après lecture/validation, au plus **1 calcul embedding** et **1 calcul rerank** sont actifs par
   worker. Une saturation embedding produit `503 {"error":"gpu_busy"}`; une saturation rerank
   produit `503 {"error":"service_busy"}`. Les deux réponses portent `Retry-After: 1`. Ces 5xx
   réutilisent les retries/fallbacks actuels, contrairement à un `429` ou un autre 4xx.
6. Le body absent ou de longueur zéro conserve le fallback historique du query-param. Un body
   composé seulement de whitespace ou un JSON syntaxiquement invalide produit
   `400 {"detail":"Invalid JSON body"}`. `{}`, `null` et `[]` restent des payloads sans valeur :
   le query-param peut les remplacer sur `/embed/query` et `/embed/single`; `/embed` et
   `/rerank` répondent avec leur erreur de contrat existante.
7. Le healthcheck Compose reste le vrai `POST /embed`, sans bypass secret ou capacité réservée.
   Sous saturation GPU, il reçoit rapidement `503 gpu_busy`; après libération, le même probe
   repasse à `200`. Cette indisponibilité est observable et ne doit pas être masquée par un
   `/health` superficiel. Le rollout devra vérifier le comportement du statut Docker sous charge.
8. Les erreurs sont des JSON courts et ne recopient ni corps, ni texte, ni secret dans la réponse
   ou les logs.
9. Le bearer statique par fichier `0600` reste la cible d'authentification. Il n'est pas activé
   dans ce lot : l'activer côté serveur seulement casserait les pipelines horaires
   `auto-discord`. Un ticket inter-projet doit rendre ce client compatible et préparer le
   cutover atomique. Le ticket SEC2 principal reste ouvert après SEC2-A.

## Critères d'acceptation

1. Les deux seuls publishes `:8003` du Compose racine sont explicitement loopback; aucun mode
   réseau hôte n'est introduit et les URLs Docker internes restent inchangées.
2. Le shim applique les limites ci-dessus aux quatre routes de calcul avant le backend, y compris
   avec un body streamé sans `Content-Length` ou volontairement lent.
3. Les tests prouvent les frontières exactes N/N+1 pour le corps, `/embed`, `/rerank`, l'ingress
   et les deux ressources, ainsi que l'absence d'appel backend sur chaque refus.
4. Les tests prouvent le vrai `POST /embed` du healthcheck sous saturation puis après récupération,
   et la libération de chaque capacité après succès ou exception. Après annulation, le lease GPU
   n'est libéré que lorsque sa tâche backend se termine; le lease ONNX reste détenu jusqu'à la fin
   physique du thread, jamais au seul départ du client.
5. Les contrats existants `/embed`, `/embed/query`, `/embed/single`, `/rerank`, `/`, `/healthz`
   et `/health` restent compatibles dans leurs cas valides.
6. Le contrat documentaire commun README/CLAUDE/architecture distingue la configuration cible
   loopback du runtime encore LAN-wide avant rollout. Il décrit les limites du shim et le
   reliquat d'auth/réseau Docker/legacy sans prétendre à une isolation WAN complète.
7. Un ticket coordonné documente le client `auto-discord` à migrer vers un bearer lu depuis un
   secret monté; un ticket séparé documente l'URL historique `red-shrik` à rendre configurable
   sans changer implicitement de modèle.
8. Des sentinelles présentes dans les corps invalides et surdimensionnés sont absentes des
   réponses et des logs capturés.
9. Les tests ciblés, la suite complète, Ruff, format, mypy, compilation du shim, Compose et
   `git diff --check` sont verts. Deux revues indépendantes du diff complet concluent `SHIP` sans
   finding P0–P3 ouvert.
10. Avant chaque commit, `gitnexus_detect_changes` confirme un rayon attendu. Après fusion, les
   deux remotes `main` pointent sur le même SHA et la pipeline GitLab de ce SHA est verte.

## Non-objectifs et frontières

- Ne pas déployer, recréer ou redémarrer le shim, le modèle, le MCP ou `auto-discord`.
- Ne pas modifier le dépôt `auto_discord` ou `red-shrik` dans cette branche.
- Ne pas activer un bearer en mode partiel, ne pas créer de secret et ne pas inscrire de token
  dans Git, les arguments de commande ou les logs.
- Ne pas retirer encore le shim du réseau partagé `hawkixs-infra`; cela exige une modification
  coordonnée du Compose `auto-discord` et la création d'un réseau client dédié.
- Ne pas modifier `deploy/dev-pc`, son supervisor ou son socket Docker dans ce lot : ce chemin
  est superseded et reste un sous-lot SEC2 distinct s'il doit être conservé comme rollback.
- Ne pas modifier les modèles, dimensions, normalisation, troncature texte ou scores.
- Ne pas prétendre fermer SEC2 globalement : l'authentification, le réseau Docker dédié et le
  reliquat supervisor restent ouverts avec propriétaires et preuves explicites.
- Les caps applicatifs de ce lot appartiennent au shim canonique. Le rollback PyTorch legacy
  devient loopback mais reste non borné, et son nom DNS ne préserve pas `auto-discord`; il ne doit
  pas être présenté comme un rollback SEC2 sûr avant un lot ingress/alias dédié.

## Découpage TDD

### Tâche 1 — Limites de requête et concurrence dans le shim

Fichiers : `services/embedding_shim/shim_app.py`, `services/embedding_shim/main.py`,
`services/embedding_shim/shim_backends.py`, `scripts/regen_embeddings.py`,
`tests/unit/test_embedding_shim.py` et les tests CLI de régénération concernés.

Avant l'édition, analyser l'impact amont de `create_app`, `_json_or_none` et des nouveaux points
d'extension pertinents; avertir avant de continuer si GitNexus retourne HIGH ou CRITICAL.

RED : ajouter les tests de sérialisation `httpx` maximale, body déclaré/streamé N/N+1, timeout de
lecture, formes JSON exactes, batch 100/101, candidats 128/129, huit lectures bloquées puis
`ingress_busy`, gates GPU et ONNX séparées, vrai probe Compose sous saturation/récupération et
libération de capacité sur tous les chemins. Utiliser `httpx.AsyncClient` + `ASGITransport`, un
backend embedding piloté par `asyncio.Event` et un backend ONNX piloté par `threading.Event`.
Après annulation du client rerank, prouver que le second calcul reste refusé jusqu'au déblocage
réel du thread. Tester directement l'ASGI pour la lecture streamée et le timeout. Consigner les
échecs réels.

GREEN : introduire un contrat de limites immuable, une lecture bornée/temporisée du body, un gate
d'ingress et deux gates de ressource limités aux routes de calcul. Aligner le CLI de régénération
sur le maximum 100 et compléter les trois annotations manquantes du shim afin que son gate mypy
soit réellement vert. Garder les réponses et le chemin nominal minimaux.

### Tâche 2 — Bind loopback et contrat documentaire

Fichiers : `docker-compose.yml`, `tests/unit/test_documentation_contract.py`, `README.md`,
`CLAUDE.md`, `docs/ARCHITECTURE.md`,
`docs/plans/2026-07-11-sol-ultra-audit-roadmap-plan.md` et ce plan.

RED : faire attendre au contrat documentaire les deux bindings loopback et le nouveau texte de
frontière; prouver que la baseline échoue encore sur `8003:8003` et la déclaration LAN ouverte.

GREEN : modifier uniquement les deux publishes racine, aligner les documents sur « code-ready,
non déployé » et valider le Compose résolu. Les réseaux et les URLs internes restent identiques.

### Tâche 3 — Coordination et preuve SEC2-A

Créer dans Brain un ticket `brain-v42 → auto-discord` pour la migration bearer + réseau client
dédié, ainsi qu'un ticket `brain-v42 → red-shrik` pour rendre l'URL QA configurable et clarifier
la propriété du modèle. Ajouter au ticket SEC2 principal les commits, compteurs, limites et
sous-lots restants; ne pas le résoudre. Consigner aussi le legacy non borné et le build CI du
Dockerfile shim comme reliquats, sans élargir silencieusement ce lot.

## Vérification

```text
uv run pytest tests/unit/test_embedding_shim.py tests/unit/test_documentation_contract.py -q
uv run pytest tests/unit/test_docker_compose.py -q
docker compose config --quiet
uv run python -m compileall -q services/embedding_shim
uv run python scripts/check_container_image_pins.py
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ services/embedding_shim
uv run pytest -q
git diff --check
git diff --check main...HEAD
```

Exécuter `gitnexus_detect_changes` avant chaque commit. Après fusion : répéter les gates
proportionnés depuis `main`, pousser sans force sur `origin/main` et `gitlab/main`, vérifier les
refs distantes puis attendre la pipeline GitLab exacte.

## Retour arrière

Un revert du ou des commits restaure les publishes et le comportement précédents. Aucune donnée,
image, ressource Docker, configuration live ou secret n'est modifié par cette livraison. Si le
bind loopback doit être appliqué ultérieurement, le runbook de déploiement devra vérifier le
trafic hôte local, le DNS Docker `auto-discord`, les quatre routes de calcul, les healthchecks et
le rollback avant tout soak.

## Preuves de livraison

- Plan critiqué par trois angles indépendants puis validé `SHIP` : commit `34ca3e5`.
- Matrice RED vérifiée sur limites, saturation et CLI : commit `210660d`.
- Shim GREEN : commit `13e53c0`; 61 tests shim/CLI ciblés passent, ainsi qu'une matrice
  embedding/reranking élargie de 174 tests incluant ces 61.
- Corps 8 MiB, JSON pathologiques, ingress 8+1, compute 1+1, erreurs/annulations et logs
  détachés sanitizés sont couverts. Mypy shim, Ruff, format et `git diff --check` passent.
- Quatre revues indépendantes — plan, sécurité, qualité et historique — concluent `SHIP` sans
  finding P0–P3 ouvert.
- Tickets de coordination créés : `9ef5c69d-cfd3-4f07-93c5-2c599ea2197b` pour `auto-discord`
  et `89140780-b853-437b-b902-86dab64cd866` pour `red-shrik`.
- Aucun déploiement, restart, secret ou réseau live n'a été créé. Le ticket SEC2 principal reste
  ouvert pour l'authentification, le réseau dédié, le rollout et les reliquats legacy/supervisor.
- Le SHA final, la concordance des remotes et la pipeline GitLab exacte seront consignés dans le
  ticket Brain SEC2, car ces preuves sont produites après ce commit documentaire immuable.
