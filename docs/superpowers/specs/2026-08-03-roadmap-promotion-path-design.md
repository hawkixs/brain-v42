# Mode link-only pour les signaux de connaissance — design

**Date** : 2026-08-03
**Statut** : validé, prêt pour plan d'implémentation
**Learning source** : `8d2b322e-5c0a-409d-96fe-9dca8a784aa9`
**Ticket connexe** : `d9a968f7-03bc-4ed5-b04b-2ba7a561abd0` (chemin SQL brut, hors périmètre)

## 1. Problème

La roadmap `brain-v42` porte 171 features, dont 71 au statut `research` actif. Sur ces 71,
64 dupliquent exactement le nom d'un artefact de connaissance existant : 48 topics de
learning, 16 titres de décision, zéro chevauchement. Les 7 restantes sont des messages de
commit promus.

Ce n'est pas un stock historique mais un flux actif. Logger une décision fait apparaître
dans la seconde une pseudo-feature portant son titre.

Le chemin exact :

```
brain_log_decision
  → DecisionService.create
  → link_artifact_if_enabled          (services/decision_service.py:135)
  → FeatureLinker.link_artifact
  → FeatureLinker._do_link_via_guard  (services/feature_linker.py:92)
  → ClusterGuard.resolve
  → _create_feature                   (services/cluster_guard.py:105)
```

`ClusterGuard.resolve()` n'expose aucun mode « lier ou ne rien faire ». Sans candidat
similaire, il crée, en nommant la feature d'après le titre de l'artefact. Même chemin pour
les learnings, snippets, runbooks et ADR.

Ce n'est pas un bug d'implémentation : c'est la feature « Feature Auto-Tracking / Roadmap »
qui fonctionne comme conçue. Le défaut est conceptuel — elle ne distingue pas un artefact de
**connaissance** (un insight, qui n'est pas un livrable) d'un signal de **travail** (un plan,
un événement GitLab, qui en est un).

La frontière est déjà tracée ailleurs dans le code. `StatusEngine.SIGNAL_STATUS_MAP` sépare
exactement les mêmes deux familles :

| Signal | Statut visé | Famille |
|---|---|---|
| `learning`, `decision`, `snippet` | `research` | connaissance |
| `runbook`, `adr` | `design` | connaissance |
| `plan` | `design` | travail |
| `mr_opened` | `building` | travail |
| `mr_merged`, `pipeline_success` | `deployed` | travail |
| `push`, `pipeline_failure` | (aucun) | travail |

## 2. Décisions de cadrage

Quatre choix validés avec l'opérateur avant conception.

**Comportement retenu : lier si match, jamais créer.** Le rattachement garde sa valeur quand
il tombe juste — c'est lui qui alimente le « 3 artifacts, dernier il y a 1j » du briefing
roadmap. On ne coupe pas le rattachement, on coupe la création.

**Périmètre : les cinq types de connaissance sans distinction.** L'option consistant à
laisser `runbook` et `adr` créer des features, au motif qu'ils décrivent un livrable, a été
écartée : deux régimes à maintenir et à documenter pour un gain marginal.

**Suppression de `merged` également.** En zone grise, l'ancien comportement concaténait le
texte de l'artefact à la description de la feature puis la ré-embeddait
(`cluster_guard.py:249-277`). Appliqué à de la connaissance, cela fait dériver l'identité
d'une feature : à force de concaténations son embedding s'éloigne de son propre sujet et
attire n'importe quoi. `link-only` signifie donc strictement « lier si confiant, sinon rien ».

**Nettoyage des 64 existantes : hors périmètre.** Corriger le robinet, mesurer le
tarissement, vider la baignoire ensuite. Purger dans la même livraison rendrait le succès
invérifiable : impossible de distinguer « le correctif fonctionne » de « la purge a masqué le
problème ». Toute nouvelle pseudo-feature apparaissant après cette livraison est un signal
direct que le correctif est incomplet.

## 3. Blast radius

`impact({target: "resolve", direction: "upstream"})` retourne **CRITICAL** : 695 symboles,
79 processus, 20 modules. Le chiffre décrit la centralité du symbole, pas la taille du
changement — `direct: 1`, et les 625 symboles de profondeur 2 sont la fermeture transitive de
la surface d'outils MCP, où tout `brain_*` traverse la couche services.

Appelants réels de `ClusterGuard.resolve()` :

| Appelant | Signaux émis | Effet du changement |
|---|---|---|
| `FeatureLinker._do_link_via_guard` | connaissance (5 types) | **cible du correctif** |
| `plan_indexer.py:362` | `plan` | aucun |
| `gitlab_ingestor.py:100` | `mr_opened`, `mr_merged`, `push`, `pipeline_success` | aucun |
| `gitlab_ingestor.py:100` | `mr_closed` | **corrigé, voir §4.1** |

Flux à surveiller en non-régression : `Brain_reindex_plans` et l'ingestion GitLab doivent
conserver leur capacité de création à l'identique, à la seule exception de `mr_closed`
documentée en §4.1.

`pipeline_failure` n'atteint jamais `resolve()` : `gitlab_ingestor` sort en amont avec
`{"status": "skipped_pipeline_failure"}`. Sa présence dans l'allowlist est donc inerte, et
conservée uniquement pour que la liste reste lisible comme « tous les signaux de travail ».

## 4. Conception

### 4.1 Allowlist de création

Une constante explicite dans `src/brain_v42/services/cluster_guard.py` :

```python
CREATING_SIGNALS = frozenset({
    "plan", "mr_opened", "mr_merged", "push",
    "pipeline_success", "pipeline_failure",
})
```

**`mr_closed` est délibérément hors allowlist**, et c'est le seul signal de travail dont le
comportement change. Il atteint bien `resolve()` et créait jusqu'ici une feature `planned`
faute de candidat. Mais la docstring de `gitlab_ingestor` documentait déjà ce signal comme
`(linking only)` : c'est l'implémentation qui contredisait son propre contrat. Une merge
request fermée n'est pas un livrable, et une feature roadmap nommée d'après son titre relève
exactement de la pollution que ce chantier supprime. Correction assumée, tranchée par
l'opérateur le 2026-08-03, et non effet de bord non vu.

**Allowlist et non denylist, par choix de fail-closed.** Un `signal_type` inconnu — nouvel
outil `brain_*`, type d'artefact ajouté plus tard — tombe en link-only et ne crée rien. Le
mode de défaillance corrigé ici est la sur-création ; le défaut penche du côté opposé. La
création délibérée conserve son chemin dédié et fail-closed, `brain_feature_create`
(décision `1e9b1929-66e0-4c2a-b3fc-aef11d759355`).

### 4.2 Règle de résolution

Hors de `CREATING_SIGNALS`, les branches qui lient sont conservées, toutes les autres
retournent `(None, "skipped")`.

| Situation | Signal de travail | Signal de connaissance |
|---|---|---|
| Aucun candidat | `created` | `skipped` |
| Cosine ≥ `COSINE_LINK` (0.70) | `linked` + promotion statut | `linked` + promotion statut |
| Zone grise, reranker ≥ `RERANKER_LINK` (0.75) | `linked` | `linked` |
| Zone grise, reranker ∈ [0.50, 0.75) | `merged` | `skipped` |
| Zone grise, reranker < `RERANKER_MERGE` (0.50) | `created` | `skipped` |
| Reranker indisponible, cosine ≥ `FALLBACK_LINK` (0.65) | `linked` | `linked` |
| Reranker indisponible, cosine < 0.65 | `created` | `skipped` |
| Cosine < `COSINE_GREY_LOW` (0.50) | `created` | `skipped` |

Aucun seuil n'est modifié.

**La promotion de statut est conservée sur `linked`.** Un learning qui matche une feature
existante à 0.70+ la fait passer `planned` → `research`. C'est un signal légitime sur une
feature qui existe déjà, et le supprimer viderait le rattachement de son intérêt.

### 4.3 Contrat de retour

```python
async def resolve(...) -> tuple[object | None, Literal["linked", "merged", "created", "skipped"]]
```

`skipped` est la seule valeur portant `None`.

`FeatureLinker._do_link_via_guard` teste `if feature is None: return 0` **avant** l'insert
dans `feature_artifacts` ; sans ce garde, l'accès `feature.id` lèverait sur `None`.

`plan_indexer` n'émet que `plan` et ne reçoit donc jamais `skipped` : le `| None` s'y traite
par une assertion explicite, jamais `# type: ignore`.

`gitlab_ingestor` **peut** recevoir `skipped`, sur `mr_closed` (§4.1). Il ne peut donc pas
se contenter d'une assertion : il stocke l'événement avec `feature_id=None` et n'insère pas
de ligne dans `feature_artifacts`. Un événement GitLab reste enregistré même sans feature à
lui rattacher.

### 4.4 Observabilité

Un log structuré `cluster_guard.skipped` portant `signal_type`, `project_key` et le meilleur
score obtenu.

Ce n'est pas du confort. La stratégie retenue en §2 reporte la purge après vérification du
tarissement, ce qui n'est vérifiable que si le tarissement laisse une trace. Ce log permet de
répondre à « combien de créations le correctif a-t-il évitées » sans requête SQL, et surtout
de détecter un `created` résiduel sur un type de connaissance — seul signe d'un correctif
incomplet.

## 5. Tests

TDD strict, RED avant toute implémentation.

**`ClusterGuard.resolve()`**

1. `signal_type="learning"`, aucun candidat → `(None, "skipped")` et aucune ligne insérée
   dans `features`
2. `signal_type="learning"`, candidat à 0.85 → `linked`, statut promu `planned` → `research`
3. `signal_type="learning"`, zone grise reranker 0.60 → `skipped`, description de la feature
   candidate **inchangée** — c'est le test qui prouve la suppression du `merged`
4. `signal_type="plan"`, aucun candidat → `created` — non-régression des signaux de travail
5. `signal_type="type_inconnu"` → `skipped` — prouve le fail-closed de l'allowlist

**`FeatureLinker`**

6. `resolve()` retourne `(None, "skipped")` → 0 lien retourné, aucune ligne dans
   `feature_artifacts`

**Intégration** — `tests/integration/test_cluster_guard_link_only.py`

Livré sous une forme plus ciblée que l'énoncé initial. Les tests unitaires mockent la
session : ils prouvent le branchement, pas que l'`INSERT` absent est réellement absent. Les
deux tests d'intégration exercent donc `ClusterGuard.resolve()` contre un vrai
PostgreSQL+pgvector et comptent les lignes de `features` après coup.

7. Signal de connaissance sur projet vide → `(None, "skipped")` et **zéro ligne** dans
   `features`
8. Signal `plan` sur projet vide → `created` et **exactement une ligne** — garde l'autre
   moitié du contrat, link-only ne doit pas devenir « ne crée plus jamais rien »

Ni GPU ni reranker requis : sur un projet vide `_find_candidates` ne retourne rien, la
résolution va droit à la décision créer-ou-skip sans appeler ces services.

Ces deux tests ont été vérifiés par mutation — ajouter `decision` à `CREATING_SIGNALS` fait
échouer le premier sur `assert 'created' == 'skipped'`. Un test d'intégration qui ne peut pas
échouer ne prouve rien, et ce lot en contenait déjà un (§5, note sur l'assertion du test de
repli reranker).

## 6. Hors périmètre

- **Les 64 pseudo-features existantes** — ticket séparé, après mesure du tarissement (§2).
- **Le chemin SQL brut `FeatureLinker._do_link`** — ticket
  `d9a968f7-03bc-4ed5-b04b-2ba7a561abd0`. Il ne crée aucune feature et ne participe donc pas
  à la pollution, mais garde une sémantique divergente : lie à *toutes* les features
  au-dessus de 0.70 au lieu de la meilleure, ignore le reranker, ne promeut aucun statut.
  L'unifier dans la même livraison rendrait le tarissement inattribuable.
- **Le statut `deployed` de la feature roadmap « Feature Auto-Tracking »**, qui mériterait
  révision après cette livraison.
- **Les seuils** `COSINE_LINK`, `COSINE_GREY_LOW`, `RERANKER_LINK`, `RERANKER_MERGE`,
  `FALLBACK_LINK`, `_DEFAULT_THRESHOLD` — aucun n'est touché.

## 7. Critère de succès

Après livraison, logger une décision ou un learning sur `brain-v42` ne crée aucune feature,
et le rattachement à une feature existante suffisamment proche continue de fonctionner et de
promouvoir son statut. Les signaux `plan`, `mr_opened`, `mr_merged`, `push` et
`pipeline_success` créent des features à l'identique ; `mr_closed` ne crée plus, par
correction assumée (§4.1).
