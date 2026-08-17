# Création explicite de features roadmap

**Date** : 2026-07-23

**Statut** : décision implémentée

**Décision Brain liée** : `1e9b1929`

**Supersède partiellement** : `2026-07-04-roadmap-curation-design.md` §2.1 et §11,
uniquement sur l'exclusivité de ClusterGuard comme writer de `features`.

## Contexte

La roadmap était alimentée uniquement par ClusterGuard à partir d'un signal déjà
capturé. Ce modèle reste adapté aux features émergentes, mais ne permet pas à un
agent ou à un opérateur de déclarer une intention de travail avant la création
d'un artefact. Fabriquer un faux signal pour obtenir cette ligne brouillerait la
provenance et rendrait le résultat dépendant d'une décision sémantique implicite.

## Décision

Ajouter `brain_feature_create` comme second chemin d'écriture volontaire :

- ClusterGuard reste le writer des signaux et conserve sa déduplication
  sémantique ;
- `brain_feature_create` crée exactement la feature demandée, sans invoquer
  ClusterGuard ;
- la création exige un `project_context` existant, des champs validés et un
  embedding exploitable ;
- un doublon exact de nom, après trim et normalisation de casse dans le même
  projet, est rejeté ;
- toute erreur contrôlée est fail-closed et ne persiste aucune feature.

La feature explicite est `pinned` par défaut pour rester visible dans la roadmap.
Le statut initial peut être l'un des statuts vivants, mais jamais `archived`.

## Concurrence et portée de l'unicité

Le service verrouille la ligne `project_contexts` du projet, revalide le projet et
le nom dans la même transaction, puis insère. Deux créations **explicites**
concurrentes du même nom donnent donc un succès et un conflit.

Cette garantie n'est pas globale : ClusterGuard ne prend pas ce verrou et la
table ne porte pas de contrainte unique sur `(project_key, lower(trim(name)))`.
Une course entre une création explicite et un signal ClusterGuard peut encore
produire deux lignes. La documentation et les réponses du tool ne doivent donc
pas promettre une unicité inter-writers.

Une contrainte SQL fonctionnelle n'est pas ajoutée dans ce lot : elle exigerait
d'abord un audit et une remédiation des doublons historiques, et elle pourrait
rejeter des features sémantiquement distinctes partageant un titre court. Un
protocole de verrou commun aux deux writers reste une amélioration de stabilité à
évaluer séparément.

## Alternatives écartées

1. **Continuer sans création explicite** : ne couvre pas le travail planifié avant
   artefact et pousse à falsifier un signal.
2. **Faire passer la demande par ClusterGuard** : le résultat pourrait être un
   lien ou un merge alors que l'appelant demande une création déterministe.
3. **Ajouter immédiatement une unicité SQL** : migration risquée sans inventaire
   des doublons et contrat produit sur les titres identiques.

## Vérification

- validation du payload et du schéma MCP ;
- échec avant embedding pour projet absent ou doublon exact existant ;
- échec avant écriture pour embedding indisponible ou invalide ;
- test PostgreSQL de deux créations explicites concurrentes ;
- suites unitaires et d'intégration complètes, Ruff et mypy avant livraison.
