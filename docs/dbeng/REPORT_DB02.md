# Field Horizon — rapport technique DB-02 « opérations autonomes »

Écrit pour : le propriétaire du dépôt et quiconque devra décider de faire confiance à un changement
autonome sur une cible PostgreSQL. Chaque affirmation renvoie à un test, un journal ou une capture
sous `_qualification/db02/` ; ce qui n'est pas démontré est dit tel quel.

Branche `db02-autonomous-operations` (depuis `windows-baseline-db01` @ `4242f6d`), **non poussée** :
aucune autorisation de publication n'a été donnée dans la session.

## 1. Point de départ vérifié

- **DB-01 revérifié indépendamment** avant toute modification : 70 tests dbeng réussis, dont 6 contre
  PostgreSQL réel (`_qualification/db02/logs/A01_dbeng_baseline.log`).
- **16 ou 17 commits ?** Les deux sont exacts : 17 commits au-dessus de `7a4fd60` = 16 commits de code
  + 1 commit de documentation (`4242f6d`), créé après la rédaction de `REPORT_DB01.md`, qui comptait 16.
  Historique non réécrit.
- **Environnements réutilisés** : Python 3.12.10 (`_qualification/windows/env`), PostgreSQL 18.6
  (binaires EDB du DB-01, `_qualification/db01/pg`), Node 22 et le Playwright du dépôt (Edge installé,
  aucun navigateur téléchargé). Aucun nouveau Python, modèle ni moteur téléchargé.
- **Ollama** est passé de 0.40.13 à **0.40.14** entre DB-01 et DB-02, par mise à jour automatique —
  pas par ce lot. Modèles inchangés (hermes3:8b, nomic-embed-text).

## 2. Ce qui a été construit

| Pièce | Fichiers | Rôle |
|---|---|---|
| Laboratoire v2 | `dbeng/bench.py`, `templates.py` | identités séparées, gardes serveur (DDL par gabarit, DML), table protégée sous un autre propriétaire, bail et registre côté cible, labo « stabilisé » (`VACUUM ANALYZE`) |
| Exécuteur privilégié | `dbeng/executor.py` | seul détenteur du secret opérateur ; commandes typées ; revérification immédiate ; bail clôturé par jeton ; preuve de propriété avant tout retrait |
| Reprise | `dbeng/recovery.py` | détection en lecture seule depuis la cible ; reprise automatique (démarrage, avant opération) ou manuelle ; attente bornée des activités orphelines |
| Opération v2 | `dbeng/operation.py`, `policy.py`, `workload.py` | pause / reprise / annulation à des points sûrs ; réautorisation juste avant le changement ; compensation confirmée ; mesure contrôlée |
| Preuves | `dbeng/export.py`, `claims.py` | intégrité, recalcul de la décision, export expurgé, affirmations des agents vérifiées |
| Service et API | `dbeng/service.py`, `views.py`, `api.py`, `server.py` | lancements idempotents, contrôles journalisés, modèles de lecture, réconciliation au démarrage, `/admin/shutdown` |
| Poste opérateur | `apps/desktop/src/db/` | écrans A–G, barre de contexte, FR/EN, clavier |
| Lanceur Windows | `scripts/windows/FieldHorizon.cmd/.ps1` | démarrage quotidien, pas de doublon, arrêt gracieux, journaux localisés |

Modèle d'autorité détaillé : `docs/dbeng/AUTHORITY_MODEL.md`. Commandes : `docs/dbeng/RUNBOOK.md`
(sections 7 à 10). Interface : `docs/gui/DB02_UI_ACCEPTANCE.md`.

## 3. Frontière de privilèges réellement obtenue

Établie empiriquement sur l'instance avant d'être codée (`_qualification/db02/logs/B01…B03`), puis
vérifiée par les tests réels :

- l'opérateur est membre du propriétaire **WITH INHERIT FALSE, SET TRUE** : sans `SET ROLE` explicite il
  n'a aucun droit de propriétaire ;
- un déclencheur d'événement `ddl_command_start` (en lecture seule, `SECURITY DEFINER`, audit par
  `RAISE LOG`) refuse avant toute création tout DDL de l'opérateur qui n'est pas exactement un gabarit
  de l'exécuteur ; un refus en `ddl_command_end` laissait un index invalide (mesuré), d'où ce choix ;
- des déclencheurs d'instruction refusent `INSERT/UPDATE/DELETE/TRUNCATE` de l'opérateur, même en tant
  que propriétaire ; `bench.audit_log` appartient à un rôle hors d'atteinte ;
- le compte de mesure n'a que `SELECT` (deux tables) et `INSERT` (une), sans propriété ni séquence.

**Pouvoir résiduel déclaré** : sous `SET ROLE`, `SELECT`, `LOCK`, `VACUUM`, `ANALYZE`, `CLUSTER` et les
gabarits `fhx_` (y compris un commentaire marqueur forgé, d'où une preuve qui ne repose jamais sur le
seul marqueur). Le secret opérateur est lisible par tout processus de l'utilisateur Windows qui exécute
le service.

**Condition non satisfaite** : une identité Windows distincte pour l'exécuteur exige un compte de
service, donc des droits administrateur indisponibles ici (utilisateur membre des Administrateurs « en
refus seulement », intégrité moyenne). Tout le reste tient sans elle ; elle reste ouverte.

## 4. Mesure : ce qui a changé, pourquoi, et quand

Les **seuils d'acceptation de la politique v1 sont inchangés**. La politique v2 ajoute des sections
(coordination, reprise, mesure) et un critère (`measurement_stable`). Trois défauts de méthode ont été
trouvés **par les mesures elles-mêmes**, diagnostiqués sur preuves, et corrigés dans la méthode — jamais
en touchant un seuil :

| Constat (preuve) | Cause démontrée | Correction |
|---|---|---|
| D01 : bruit A/A 20,6 % (> 15 %) et régression apparente d'une table que l'index ne touche pas → **REVERTED** | autovacuum des tables fraîchement chargées pendant la mesure (`last_autovacuum` 19:56:24–19:56:30, mesure 19:55:45–19:56:12) | `bench-setup` se termine par `VACUUM (ANALYZE)` ; chaque phase attend (borne 180 s) la fin de toute maintenance et le consigne |
| E01–E02 : la phase « après » systématiquement plus lente, jusqu'aux contrôles | les écritures mesurées sont annulées mais laissent des tuples morts (82 000 / phase ; 22 % d'une table de 300 000 lignes) : biais contre tout changement | VACUUM typé des tables du labo avant chaque phase (`measurement.vacuum_before_phase`, opération `vacuum_table`) |
| E02 : bimodalité ×1,6–1,7 par salves, sur toutes les requêtes, contrôles compris | migration des processus serveur entre les deux CCD du Ryzen 9 7900 (perte du cache L3) | option `-CpuMask 0xFFF` du script d'instance : le serveur de test (et lui seul) épinglé sur un CCD ; contrôles ensuite à ±1–3 % |

La politique a donc changé de SHA-256 pendant le développement : D01/D02 ont tourné sous un brouillon
v2 (`bec66b78de43…`), toutes les exécutions d'acceptation sous la v2 finale
`043ca97858c80b7c71bb79bf2008cb0eb98f98962a37a6822099dadac83b7b4e`. Chaque opération archive sa
politique et sa charge dans ses preuves, ce qui permet de recalculer sa décision.

Mesures produites : temps serveur (EXPLAIN ANALYZE, base de la décision), temps client (instruction
simple, rapporté), coût pondéré (poids × médiane : un coût, pas un débit), impact en écriture (requêtes
d'écriture, seuil propre), stockage (taille d'index, variations de taille), effets de bord (tuples
morts, valeurs d'identité consommées), maintenance observée.

## 5. Résultats

### 5.1 Tests

| Suite | Résultat | Preuve |
|---|---|---|
| Python complète (unitaires, API, CLI) | **1 398 réussis, 16 ignorés** (les tests PostgreSQL sans variables), 1 min 32 s | `logs/E07_pytest_full_suite_final.log` |
| PostgreSQL réel (15 tests, instance épinglée) | **15/15**, deux fois (avant et après les dernières corrections) | `logs/E04_pytest_postgres_full.log`, `logs/E08_pytest_postgres_final.log` |
| dbeng en mémoire (opération, API, unités) | 91 réussis | même suite |
| Interface (Vitest) | **59/59** (15 fichiers) | `npx vitest run` |
| Types (mypy paquet, tsc), lint (ruff) | propres | — |
| Contrat OpenAPI / types TS | régénérés, sans dérive | `tests/test_openapi_contract.py` |
| Fumée de l'exécuteur sur le labo | 14/14 | `logs/C02_executor_smoke.log` |

Un test historique a révélé une régression de ma part pendant le lot (`run_server` ne passait plus par
`uvicorn.run` : le test démarrait un vrai serveur et la suite se figeait) ; corrigée, la suite
complète passe. Les 124 erreurs mypy des *autres* fichiers de tests sont antérieures à DB-02 (aucune
dans les tests dbeng) et restent hors de ce lot.

### 5.2 Opérations réelles (labo `fh_bench`, 2 000 000 commandes)

| Opération | Charge | Statut | Fait marquant |
|---|---|---|---|
| `OP-20261103T195545-e2af96` (D01) | lecture dominante | REVERTED | refus prudent sous bruit (autovacuum), compensation confirmée |
| `OP-20261103T195955-c42e7f` (D02) | lecture dominante | **RETAINED** | médiane 66,2 → 0,028 ms ; coût pondéré ×0,099 ; contrôles à ±2 % |
| `OP-20261103T210711-73634e` (UI, Edge, final) | lecture dominante, **mise en pause puis reprise depuis l'interface** | **RETAINED** | pause effective au point sûr, bail conservé |
| `OP-20261103T210747-9dbb08` (UI, Edge, final) | écriture dominante | **REVERTED** | coût pondéré ×2,1 ; `bench.orders(note)` retiré, absence confirmée dans le catalogue et les plans |
| `OP-20261103T210821-a5feca` (CLI) | écriture dominante, `--fault-after apply` (code 86) | interrompue → **RECOVERED_REVERTED** | affichée « interrompue » (pas « en cours ») ; reprise manuelle depuis l'interface |
| `OP-20261103T211050-0a451e` (UI, Tauri) | lecture dominante, pause puis reprise | **RETAINED** | même parcours dans l'application native |
| `OP-20261103T211123-9f6951` (UI, Tauri) | écriture dominante | **REVERTED** | compensation confirmée |
| `OP-20261103T211603-0d2878` (D03, CLI, **agents LLM réels**) | écriture dominante, hermes3:8b | **REVERTED** | agents consultés, aucun n'a pesé sur la décision ; le validateur (« revert ») est d'accord avec la décision déterministe, effet nul |

Un premier passage des parcours (`journeys_edge/`) a révélé qu'une opération dont le processus est mort
restait affichée « en cours » avec des contrôles inopérants : corrigé (classification « interrompue »,
contrôles refusés par l'API, `DB_OPERATION_INTERRUPTED`), puis tous les parcours ont été rejoués.

### 5.3 Agents LLM réels et vérification de leurs affirmations

D03 a fait tourner les cinq rôles sur hermes3:8b (Ollama 0.40.14) : 3 affirmations acceptées (citant des
preuves existantes), 2 propositions de l'agent planificateur — toutes deux passées par la politique
comme n'importe quel candidat (`bench.audit_log(id, event)` : table protégée et colonne inexistante ;
`bench.customers(id)` : déjà indexée). **La vérification a révélé un défaut du vérificateur** : deux
affirmations fausses (« bench.orders n'est pas analysée et n'a aucun index », « bench.audit_log est lue
séquentiellement ») avaient été marquées « vérifiées » sur la seule existence des tables nommées.
Corrigé (une existence seule ne vérifie plus rien ; reconnaissance étendue : lecture séquentielle,
table sans index, table non analysée ; deux tests ajoutés). Recalcul **séparé** depuis les preuves
archivées (`_qualification/db02/logs/D03_claims_recheck.json`, le `claims.json` d'origine est
inchangé) : 4 vérifiées / 1 contredite / 1 non vérifiable → **2 vérifiées / 3 contredites / 1 non
vérifiable**. Aucune de ces vérifications n'entre dans une décision.

## 6. Les dix critères

Statuts : **testé en réel** (sur l'instance, mesuré) · **testé auto.** (en mémoire seulement) ·
**bloqué** · **non couvert**.

| # | Critère | Statut | Preuves |
|---|---|---|---|
| 1 | Optimisation utile sans approbation intermédiaire | **testé en réel** | D02 ; parcours UI « optimisation » ; `test_05` |
| 2 | Changement nuisible annulé automatiquement | **testé en réel** | parcours UI « régression » ; `test_07` ; D01 (prudence sous bruit) |
| 3 | Demandes hors autorité refusées sans modification | **testé en réel** | `test_03` (16 refus serveur + 3 du compte de mesure, aucun résidu, comptes de lignes inchangés) ; `test_04` (11 codes de refus de l'exécuteur) ; `test_01`, `test_02` ; API : aucun champ SQL, chemin de charge refusé |
| 4 | Deux processus indépendants coordonnés | **testé en réel** | `test_08` (deux CLI, deux journaux : le second BLOCKED `lease_busy`) ; `test_13` ; `test_15` (cinq clics, une opération) |
| 5 | Interruption pendant le DDL puis reprise correcte | **testé en réel** | `test_09` (kill pendant la construction ; le serveur la termine ; preuve « construction interrompue » ; le journal du processus tué est clos par sa propre reprise) ; `test_10` (crash après construction ; reprise automatique au démarrage ; idempotente) ; `test_11` (transaction longue → `ddl_lock_timeout` → reprise) ; `test_12` |
| 6 | Décisions liées aux mesures d'origine, exports intacts | **testé en réel** | vérification depuis l'interface (intégrité « intacts », recalcul identique, attestation « aucune ») ; export expurgé ; altération détectée (`test_evidence_integrity…`) |
| 7 | Quatre parcours UI, navigateur et Tauri | **testé en réel** | Edge et application Tauri native : premier lancement, optimisation (avec pause/reprise), régression, incident — opérations réelles, 0 erreur console ; `docs/gui/DB02_UI_ACCEPTANCE.md` |
| 8 | Présentation propre aux résolutions et échelles | **testé en réel (émulé)** | 36 captures, 0 débordement mesuré ; mise à l'échelle Windows émulée par le facteur de pixel, l'affichage système n'a pas été modifié |
| 9 | Pas de régression des fonctions historiques | **testé en réel** | suite complète 1 398 ; écrans corpus sous « Avancé » ; `/database` redirige vers `/db` |
| 10 | Lancement Windows documenté et rejoué | **testé en réel** | `logs/G01…G06` (doublon refusé, démarrage, statut sous PowerShell 5.1 et 7, arrêt gracieux, redémarrage) ; RUNBOOK §8 |

## 7. Capacités

| Capacité | Statut |
|---|---|
| Rôles séparés, gardes serveur, table protégée | implémenté, testé en réel |
| Exécuteur typé, revérification, codes de refus | implémenté, testé en réel |
| Bail cible + jeton de clôture, deux processus | implémenté, testé en réel |
| Registre côté cible, preuve de propriété | implémenté, testé en réel |
| Reprise automatique (démarrage, avant opération), manuelle, idempotente | implémenté, testé en réel |
| Pause / reprise / annulation, sémantique affichée | implémenté, testé en réel (pause/reprise depuis l'UI) ; annulation testée en mémoire et via API |
| Lancement idempotent (double clic, rafraîchissement, nouvel essai) | implémenté, testé en réel (`test_15`) et en UI |
| Mesure : contrôles, dérive, VACUUM par phase, attente de maintenance, temps client | implémenté, testé en réel |
| Profils lecture / écriture / mixte | implémenté ; lecture et écriture testés en réel, mixte défini |
| pg_stat_statements (observé, jamais rejoué) | implémenté, affiché en réel (écran Cible) |
| Affirmations des agents vérifiées | implémenté, testé en réel (D03, hermes3:8b) — défaut trouvé par ce test réel, corrigé, recalcul séparé (§5.3) |
| Export expurgé, intégrité, recalcul, absence d'attestation affichée | implémenté, testé en réel |
| Interface FR/EN, clavier, contrastes AA | implémenté, testé (Vitest, captures) |
| Lanceur Windows | implémenté, testé en réel |
| Identité Windows séparée pour l'exécuteur | **bloqué** (droits administrateur) |
| Installeur Windows (optionnel) | **non couvert** : cibles de paquet configurées Linux uniquement ; un installeur NSIS imposerait le téléchargement d'un nouvel outil |
| CI distante | **définie et exécutée localement job par job, non observée** sur GitHub Actions (rien n'est poussé) |

## 8. Notes honnêtes et limites

- L'émulation de l'échelle d'affichage (facteur de pixel du navigateur) n'est pas un changement réel
  du réglage Windows ; elle reproduit la taille CSS visible à 125 % et 150 %.
- La classification « interrompue » d'une opération repose sur la vie du PID enregistré ; un PID réutilisé
  par un autre processus la ferait paraître vivante (la reprise, elle, s'appuie sur le bail de la cible).
- Le message journalisé d'une opération est écrit en anglais par le moteur ; l'interface l'affiche
  comme tel, étiqueté « message journalisé », à côté de la cause traduite.
- Le « rapport des requêtes cibles après compensation » (×1,31 pour D03-type) est un seul tour, sans
  échauffement, juste après le retrait : il confirme le retour au plan séquentiel, il n'est pas une
  mesure de performance et n'entre dans aucun critère.
- La reconnaissance des affirmations des agents reste littérale : ce qu'elle ne sait pas tester est
  « non vérifiable », jamais deviné.

## 9. Commits (non poussés)

Voir `git log windows-baseline-db01..db02-autonomous-operations`. Aucune fusion dans `main`, aucun
force-push, aucune publication.
