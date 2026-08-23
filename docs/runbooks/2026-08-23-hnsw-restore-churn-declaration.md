# Déclaration — ce qu'une restauration change, et ne change pas, dans une recherche sémantique

**Mesuré le 2026-08-23 sur les embeddings RÉELS de la production `brain-v42`.**
Ticket `cfd26e9d`. Cette page est une DÉCLARATION, pas une procédure de réparation :
elle sert à décider, après un restore, si un écart observé est du bruit ou une panne.

---

## 0. À lire avant tout : aujourd'hui, la question ne se pose pas

**Aucun des 9 index HNSW n'est emprunté par la production.** Mesuré le 2026-08-23 :

```
select s.relname, s.indexrelname, s.idx_scan
from pg_stat_user_indexes s
join pg_class c on c.oid = s.indexrelid
join pg_am    a on a.oid = c.relam
where a.amname = 'hnsw'
order by s.idx_scan desc;
```

→ `idx_scan = 0` sur les neuf, après 12 j 21 h d'uptime. Sur la MÊME table au MÊME
instant, `learnings_pkey` compte 894 194 parcours et l'index FTS GIN 5 071 : le compteur
fonctionne, l'index HNSW n'est simplement jamais atteint.

Le planificateur préfère un `Seq Scan` + tri exact : sur `learnings`, 620 contre 1 827
pour le parcours HNSW forcé. Une seule table, `indexed_plan_chunks`, choisit HNSW sur une
requête nue — mais sa vraie requête de production porte une jointure obligatoire vers
`indexed_plans` et deux filtres de statut, et retombe sur `Hash Join` + deux `Seq Scan`.

**Conséquence** : la recherche sémantique de `brain-v42` est aujourd'hui un KNN EXACT par
force brute. Le non-déterminisme de reconstruction HNSW **n'a aucun effet sur les résultats
rendus à un humain.** Le churn décrit plus bas est CONDITIONNEL — il n'existe que si l'index
redevient emprunté.

### Quand cette déclaration cessera d'être vraie

Le basculement est piloté par le nombre de PAGES, donc par la taille du corpus. Mesuré par
insertions successives sur une copie jetable de la production :

| `learnings` | relpages | plan choisi |
|---|---|---|
| 3 167 (aujourd'hui) | 512 | `Seq Scan` |
| 5 567 | 863 | `Seq Scan` |
| **5 967** | **924** | **`Index Scan using idx_learnings_embedding`** |

**Seuil : ~5 800 lignes sur `learnings`, contre 3 167 aujourd'hui** — environ 82 % de marge.
À 13,4 lignes/jour (rythme mesuré sur 90 jours) cela fait ~6,5 mois ; au rythme d'août
(606/mois) ~4,5 mois. **Le déclencheur est le COMPTE DE LIGNES, pas la date.**

> **Contrôle à refaire avant de se fier à cette page** : rejouer la requête `idx_scan`
> ci-dessus. Si un seul des neuf compteurs est non nul, la section 0 est périmée et les
> sections 1–3 deviennent le régime courant.

---

## 1. Ce qu'une restauration ne change JAMAIS

`pg_dump` ne transporte **aucun graphe** : le TOC de l'archive porte 9 entrées `INDEX` et
zéro octet de graphe HNSW. Les neuf index sont **reconstruits** à la restauration.

Malgré cela, sur le chemin EXACT — celui que la production emprunte :

| Comparaison | recouvrement top-10 | même ENSEMBLE | même ORDRE |
|---|---|---|---|
| restore A vs restore B | **10,000 / 10** | 1544/1544 | 1544/1544 |
| production vs restore | 9,997 / 10 | 1540/1544 | 1518/1544 |

Les 26 requêtes qui diffèrent entre production et restore diffèrent **à 100 % par
départage d'égalités** : la séquence des 10 distances est identique au chiffre près
(`max |Δd| = 0.000e+00`). Deux lignes à distance rigoureusement égale sortent dans l'ordre
du tas, et un tas fraîchement restauré est compacté autrement qu'un tas vieux de huit mois.

> **Aucune perte sémantique. Zéro.** Un ordre qui bouge entre deux lignes à égalité parfaite
> n'est pas une dégradation.

---

## 2. Le churn CONDITIONNEL, si l'index redevient emprunté

Mesuré en forçant le parcours d'index (`set enable_seqscan = off`), sur les embeddings réels,
**n = 1 544 requêtes**, top-k = 10, corpus = les 9 tables (7 555 vecteurs au total).

### 2.1 Le bruit de reconstruction — sept mesures indépendantes

| Reconstruction | recouvrement | même ENSEMBLE |
|---|---|---|
| `reindex` passe 1 | 9,847 | 96,6 % |
| `reindex` passe 2 | 9,901 | 96,9 % |
| `reindex` passe 3 | 9,814 | 96,2 % |
| `reindex`, `max_parallel_maintenance_workers=0` | 9,842 | 96,4 % |
| `reindex`, `max_parallel_maintenance_workers=2` | 9,869 | 96,8 % |
| `drop` + `create` | 9,836 | 96,6 % |
| **restore A vs restore B** | **9,866** | **96,8 %** |

**BANDE DE BRUIT : 9,81 – 9,90 de recouvrement, 96,2 – 96,9 % de requêtes au top-10 identique.**

Deux restaurations indépendantes du MÊME dump tombent à 9,866 — **à l'intérieur** de la bande.
Une restauration n'ajoute donc **rien** au bruit d'une simple reconstruction.

Désactiver la construction parallèle ne corrige rien (9,842 contre 9,869) : le régime
séquentiel est marginalement le plus instable des deux.

### 2.2 Ce n'est pas le graphe qui bouge, ce sont les égalités

En comparant les DISTANCES et non les identifiants :

| Comparaison | requêtes qui diffèrent | dont pur départage d'égalité | dont churn réel | `max abs delta d` |
|---|---|---|---|---|
| reconstruction vs restore | 69 / 1544 (4,5 %) | **84,1 %** | 15,9 % | 6,7e-03 |
| production vive vs restore | 227 / 1544 (14,7 %) | 63,0 % | 37,0 % | 3,0e-01 |

Les tables riches en doublons churnent le plus, et ce churn est **entièrement fictif** :

| table | vecteurs distincts | churn de reconstruction | dont égalités |
|---|---|---|---|
| `gitlab_events` | 166 / 239 (69,5 %) | 8,85 / 10 — le pire | **100 %** |
| `indexed_plans` | 180 / 199 (90,5 %) | ensemble stable, ordre non | **100 %** |
| `learnings` | 3 061 / 3 167 (96,7 %) | 10,00 / 10 | — |
| `snippets`, `runbooks`, `adrs` | 100 % | 10,00 / 10 | — |

`gitlab_events` a la pire apparence et la distance moyenne la plus stable de tout le corpus
(0,198894 sur les trois états, au chiffre près). Son « churn » est le brassage de titres
identiques — « Merge branch… » — vectorisés à l'identique.

### 2.3 Un cas qui SORT de la bande, et ce n'est pas la restauration

| Comparaison | recouvrement | même ENSEMBLE |
|---|---|---|
| **production vive vs restore** | **9,70 – 9,73** | **90,2 %** |

L'index de production est maintenu **incrémentalement** depuis des mois ; un restore le
**reconstruit en masse**. Ce sont deux graphes différents, et l'écart est réel — 37 % des
divergences déplacent une vraie distance, jusqu'à 0,30. C'est le chiffre qu'un opérateur
constaterait, et il est **plus grand** que l'écart entre deux restaurations.

---

## 3. Distinguer le bruit d'une vraie dégradation

### La règle

> **Ne comparez JAMAIS des listes d'identifiants.** Jusqu'à 15 % des requêtes rendent des
> identifiants différents sans qu'aucune distance n'ait bougé.
> **Comparez le NOMBRE de lignes rendues, puis la DISTANCE MOYENNE du top-10.**

### Les trois signaux, avec leurs bandes mesurées

| Signal | Sain (mesuré) | En panne (mesuré) |
|---|---|---|
| lignes rendues par requête | **10,000** | 1,095 (`ef_search` effondré) |
| distance moyenne du top-10 | **0,31017 – 0,31027** | 0,31670 (`m=4`, `ef_construction=8`) |
| rappel vs KNN exact | **0,974 – 0,979** | 0,820 |

La distance moyenne bouge de 1,0e-04 sur toute la bande saine et de **6,5e-03 sur une vraie
dégradation : le signal vaut ~60 fois le bruit.** Le nombre de lignes rendues attrape à lui
seul l'effondrement d'`ef_search`, que la distance moyenne ne montrerait pas.

### La sonde opérateur

À jouer sur la base restaurée, puis sur une référence. Aucune écriture.

```sql
-- Remplacer <TABLE> et coller un vecteur de requête réel (1536 flottants).
-- Jouer DEUX fois : tel quel (chemin nominal), puis avec `set enable_seqscan = off`
-- (chemin HNSW forcé). Comparer les deux sorties.
set extra_float_digits = 3;
select count(*)              as lignes_rendues,   -- attendu : 10
       round(avg(d)::numeric, 6) as distance_moyenne
from (
  select embedding <=> '[…]'::vector as d
  from <TABLE>
  where embedding is not null
  order by embedding <=> '[…]'::vector
  limit 10
) s;
```

### Lecture

1. `lignes_rendues < 10` → **panne**. L'index rend moins de candidats que demandé.
2. `distance_moyenne` **supérieure de plus de 1 %** à la référence → **panne**
   (une vraie dégradation `m=4` donne +2,1 % ; le bruit de reconstruction donne +0,03 %).
3. `distance_moyenne` dans ±0,1 % et 10 lignes rendues → **bruit de reconstruction**, même si
   la moitié des identifiants ont changé. Ne cherchez pas de corruption.
4. Comparaison décisive quand aucune référence n'est disponible : rejouer la MÊME requête
   avec `set enable_indexscan = off` (KNN exact, déterministe) et comparer la distance
   moyenne. **L'écart exact↔HNSW est le rappel** ; c'est la seule mesure absolue.

### Contrôle de la sonde, joué dans les deux sens

La sonde a été cassée puis réparée, pour prouver qu'elle réagit :

| État | distance moyenne | lignes | rappel | recouvrement d'identifiants |
|---|---|---|---|---|
| restore sain | 0,310173 | 10,000 | 0,9742 | référence |
| **cassé** `m=4, ef_construction=8` | **0,316698** | 10,000 | **0,8196** | **8,222 / 45,9 %** |
| **réparé** `m=16, ef_construction=64` | **0,310172** | 10,000 | **0,9727** | **9,836 / 96,6 %** |

Casser → la sonde sort de la bande. Réparer → elle y revient.

---

## 4. Pourquoi le chiffre synthétique ne devait pas être publié

Une mesure antérieure sur vecteurs **synthétiques uniformes** annonçait un bruit de
reconstruction de **8,60 / 10, 4 requêtes sur 20 inchangées (20 %)**.

Sur embeddings réels, le bruit est de **9,87 / 10 et 96,8 % de requêtes inchangées.**

Et surtout — un index **réellement dégradé** (`m=4`) mesure **8,222 / 10 et 40,9 %
d'identifiants inchangés.**

> **Le chiffre synthétique était PIRE qu'une panne réelle.** Publié comme « normal après un
> restore », il aurait rendu invisible exactement la dégradation qu'il fallait voir. C'est le
> second mode de panne — la vraie dégradation couverte par le bruit déclaré — et il était à
> deux dixièmes de point d'être gravé dans ce runbook.

---

## 5. Voisin constaté, non traité ici

La production porte `extversion = '0.8.2'`, mais son `vector.so` a le md5
`5cfaddef0e7c4931811a3384466259c3`, **identique au binaire de l'image `0.8.4-pg16`** et
différent de celui de `0.8.2-pg16` (`5c971952ab066b12175bad518d32de79`). L'image ne porte
qu'UN `vector.so`, qu'UN script d'extension, et `pg_available_extension_versions` n'y rend
que `0.8.4`.

**Une base restaurée depuis cette image déclare `extversion = '0.8.4'`.** Un contrôle de
contrat qui compare l'étiquette d'extension source à celle de la cible **échouera donc sur une
restauration parfaitement saine**. Constaté au passage ; le contrat DR n'est pas modifié ici
(ticket `2ed0d4e0`).
