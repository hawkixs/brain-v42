---
title: "Disaster recovery vérifiable — preuve opérationnelle B3"
status: active
summary: "Cycles DR-v1 historiques authentifiés et restore PostgreSQL head 037 acquis ; rôles/ACL, rebuild Neo4j dédié, off-host chiffré, alerte et activation DR-v5 restent ouverts."
tags:
  - disaster-recovery
  - red-backup
  - systemd
  - operational-evidence
  - sol-ultra
---

# Disaster recovery vérifiable — preuve opérationnelle B3

> **Amendement de sûreté — 24 juillet 2026.** La décision Brain
> `3d3d72e4-acb7-49fe-aabb-1618e648e627` remplace la preuve de restauration Neo4j exacte par
> l'option A. La preuve obtenue au head 035 reste historique depuis le passage de la production à
> 037 : elle ne fermait plus le gate courant. Le run DR-v5 `20260724_150315` fournit désormais la
> preuve isolée PostgreSQL au head exactement déployé. La reconstruction d'une projection Neo4j
> dédiée et vide avec le protocole graph introduit en 035 reste un gate séparé. Ne jamais
> downgrader pour suivre ce checkpoint.

Ce checkpoint complète le
[plan DR](2026-07-11-disaster-recovery-verified-implementation-plan.md) sans déclarer DR1
déployé. Il distingue les runs manuels des déclenchements du timer.

## Preuves acquises les 12 et 13 juillet 2026

Le timer `red-backup.timer` est `enabled`, `active` et `waiting`. Son unité live impose
`OnCalendar=*-*-* 03:00:00`, `Persistent=true` et `RandomizedDelaySec=300`. Systemd enregistre
deux déclenchements automatiques consécutifs : le 12 juillet à `03:02:22 CEST`, puis le
13 juillet à `03:00:09 CEST`.

Le premier déclenchement a produit le run `20260712_010222` sous `/data/backups`. Le journal
rapporte `7/7 targets` en `30.9s`. La commande suivante l'authentifie sans mutation :

```text
red-backup verify-run 20260712_010222
[OK] completeness=complete; 7 targets; 42 artifacts
```

Le reçu canonique porte le SHA-256
`97efd0e0b33fec4bb16aba51ecdaa9cde1ced77466280f3908092256b0b51e53`. Le marker
`.complete` contient exactement ce SHA. Le répertoire est en mode `0700`; le reçu et le
marker sont en `0600`.

Le second déclenchement a produit le run `20260713_010009`. Le journal rapporte `7/7 targets`
en `32.4s`, et sa vérification indépendante aboutit au même inventaire :

```text
red-backup verify-run 20260713_010009
[OK] completeness=complete; 7 targets; 42 artifacts
```

Son reçu canonique porte le SHA-256
`edc12c2fe42f1c9100380e176dd52e318100ba41e5654b51c7567b2ae6debd1f`. Le marker
`.complete` correspond à ce SHA. Le répertoire est en mode `0700`; le reçu et le marker sont
en `0600`.

## État contrôlé le 14 juillet 2026

Le déclenchement automatique du timer a produit le run `20260714_010021`. Son reçu lie ce
run à la politique `red-backup-dr-v1`; la vérification indépendante confirme `7 targets` et
`42 artifacts`. Il prolonge donc la preuve automatique DR-v1, mais ne constitue pas une
preuve de déclenchement automatique sous DR-v2.

Le run `20260714_072607`, lancé manuellement sous la politique `red-backup-dr-v2`, est
complet et vérifié avec `8 targets` et `44 artifacts`. Il inclut la cible
`red-writer-media`, absente de DR-v1. Il prouve le chemin DR-v2 et son autorité de
récupération, pas son exécution planifiée.

Le lot B3 de `red-backup` est fusionné et poussé sur `main` au commit `6b85657` depuis le
commit de feature `342d8d1`. La suite complète relancée sur `main` aboutit à
`1272 passed, 4 skipped`. Une exécution live en lecture seule du watchdog, avec un seuil
maximal de `25h30`, retourne `fresh` pour le run DR-v2 `20260714_072607`. Cette preuve valide
le calcul de fraîcheur et la revérification des artefacts présents; elle n'est pas un
déclenchement systemd du watchdog.

Le code de chargement sécurisé des credentials et les unités
`red-backup-watchdog.service` / `red-backup-watchdog.timer` sont implémentés et versionnés.
Le timer cible `04:45`, avec une grâce de démarrage de `100min` et `Persistent=false` pour
ne pas rejouer après coup un contrôle calendaire manqué. Le service principal déclare
`OnSuccess` et `OnFailure` vers le watchdog afin de déclencher un contrôle après chaque issue
du backup. Les unités DR-v3 sont installées depuis le 22 juillet et le watchdog événementiel
est vert ; le timer watchdog quotidien reste désactivé. Aucun webhook n'est provisionné, et
aucune réception Discord n'est donc prouvée.

## Preuve PostgreSQL head 037 acquise le 24 juillet 2026

L'autorité `red-backup-dr-v5`, SHA-256
`6cb6b5e7a8805151301ab76ce94fe885cfc476bc370252848b7294767ab549e0`, conserve le contrat Brain
v3 et utilise une attestation distincte qui neutralise uniquement la redécomposition textuelle de
casts de tableaux par `pg_restore`. Le run explicite `20260724_150315` est complet : huit cibles,
47 artefacts et `verify-run` vert.

Le drill PostgreSQL 16 restaure Brain au head 037 et passe les 24 contrôles. L'attestation SQL
indépendante, SHA-256 `d46bcdbbc1e560bb7859ddfff9883572fd4f6462cc38732520dd880d3155fd6a`,
concorde exactement. Le rapport privé
`/data/backups/.drills/20260724_150315/brain-v42/5f0dc90b347b2de2b9ec4b210dafa004.json`
atteste aussi le nettoyage complet du conteneur et des volumes jetables.

Le round-trip MinIO passe sur 33 objets et 52 832 376 octets, avec l'inventaire
`0cce7e6da277aac74190eff4dcc78f38f57ba3cb3758563eeaf5749168bbeeab`; aucun conteneur, réseau ou
workspace temporaire ne reste. Le watchdog explicite retourne `fresh` sur le tuple v5 exact.

Le cron utilisateur DR-v1, qui avait encore produit le run `20260724_030001`, a été retiré. Le
timer live DR-v3 est ensuite revenu `enabled`, `active` et `waiting`, prochaine échéance
`2026-07-25 03:00:03 CEST`. Les unités restent intentionnellement en DR-v3 : un cycle planifié et
une activation sous DR-v5 constituent une livraison ultérieure.

## Limite de la preuve

Le service a aussi réussi à `01:52:14 CEST`, mais le timer n'a été démarré qu'à
`01:54:12 CEST`. Ce run était manuel et ne compte pas comme cycle automatique. La preuve de
deux cycles automatiques consécutifs repose exclusivement sur les runs `20260712_010222` et
`20260713_010009`; elle est désormais acquise.

DR1 reste `building`. Le restore PostgreSQL isolé au head 037 est désormais acquis. Restent ouverts :
la remise en place post-restore des rôles, propriétaires et ACL, un rebuild Neo4j dédié et vide,
la copie off-host chiffrée et la livraison d'une alerte Discord.

## Prochaines vérifications

Les preuves restantes sont indépendantes des deux cycles systemd historiques :

1. activer DR-v5 dans une livraison distincte et authentifier un déclenchement automatique ;
2. prouver la remise en place isolée des rôles, propriétaires et ACL, puis un rebuild complet dans
   une base Neo4j dédiée et vide ;
3. authentifier une copie off-host chiffrée ;
4. installer puis activer le watchdog quotidien, et provoquer puis recevoir une alerte Discord de
   backup en échec.
