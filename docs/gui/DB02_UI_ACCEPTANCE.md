# DB-02 — acceptation de l'interface du poste d'exploitation

Écrit pour : le propriétaire, pour juger le poste opérateur sur pièces. Toutes les captures ci-dessous
sont des captures **réelles** : service réel, base PostgreSQL 18.6 réelle (labo synthétique de
2 000 000 commandes), opérations réellement exécutées depuis l'interface. Aucune maquette. Le jeton
n'apparaît dans aucune capture (champ masqué, jamais saisi visiblement), ni le chemin du fichier de
secrets.

Statut de DB-02 : version expérimentale, validée seulement sur une instance PostgreSQL isolée ; pas
prête pour une exploitation autonome en production (`docs/dbeng/REPORT_DB02.md`).

Une seule retouche avant publication : dans `08_incident_detected.png` et
`12_tauri_incident_recovered.png`, le nom de l'ordinateur, que l'interface affiche dans le texte de
l'incident (« left in state 'applied' by … »), est recouvert d'un rectangle gris. Aucun autre pixel
n'est modifié : la comparaison pixel à pixel avec l'original ne trouve de différence que dans ce
rectangle. Les originaux restent hors Git, sous `_qualification/db02/captures/`.

Captures sélectionnées : `docs/gui/db02/`. Jeu complet et journaux d'exécution :
`_qualification/db02/captures/` (hors Git). Scripts rejouables :
`_qualification/db02/tools/run_journeys.sh`, `ui_journeys.cjs`, `ui_matrix.cjs`, `ui_details.cjs`.

## 1. Écrans

| Écran | Route | Contenu |
|---|---|---|
| A — Exploitation | `/db` | état de la cible (identité, connexion, bail, incidents, mode de reprise, dernière opération), lancement idempotent ou opération en cours avec contrôles, incidents et reprise manuelle, opérations récentes, index Field Horizon rapprochés du registre, réconciliation au démarrage |
| B — Cibles | `/db/targets`, `/db/targets/:nom` | identité épinglée contre identité en direct, identités, **frontière de privilèges lue en direct dans les catalogues** (rôles, appartenances INHERIT/SET, gardes, propriétaires, privilèges), pouvoir résiduel déclaré, trafic observé (pg_stat_statements, jamais rejoué), dernier instantané |
| C — Opérations | `/db/operations` | filtres (statut, type, texte), tri, pagination |
| D — Fiche opération | `/db/operations/:id` | synthèse, contrôles et leur historique, onglets Chronologie / Décision / Mesures (chaque échantillon, échelle log, tableau équivalent) / Autorité et changement (revérification, compensation, échec, reprise) / Agents (affirmations vérifiées) / Preuves (intégrité, recalcul, export expurgé, rapport) |
| E — Workloads et performances | `/db/workloads` | charges versionnées (requêtes, poids, contrôles), historique par charge, méthode de mesure |
| F — Preuves et audit | `/db/evidence` | ce qui est prouvé ou non (intégrité, recalcul, attestation : aucune, expurgation), vérification et export par opération |
| G — Paramètres | `/db/settings` | langue, emplacements, configuration du fichier de secrets (oui/non, jamais le chemin), sens des contrôles, politique en vigueur (lecture seule) |

La barre de contexte, présente sur tous ces écrans, montre : cible, environnement déclaré, identité
vérifiée, connexion, fraîcheur, politique active (version + empreinte), opération en cours.
Les écrans du moteur corpus restent accessibles sous « Avancé — corpus ».

## 2. Les quatre parcours — navigateur (Edge)

Exécution finale : `_qualification/db02/captures/journeys_edge_final/` (labo réinitialisé juste avant).

| Parcours | Ce qui s'est réellement passé | Opération | Captures |
|---|---|---|---|
| 1. Premier lancement | écran du jeton → Exploitation avec la cible vérifiée → réconciliation au démarrage (mode automatique, « rien à reprendre ») | — | `edge_first_01…03` |
| 2. Optimisation | lancée depuis l'interface (identifiant de requête) ; **pause** demandée pendant la mesure de référence, prise en compte au point sûr suivant (« baseline round 2/4 »), bail conservé ; vue d'ensemble pendant la pause ; **reprise** ; résultat **Conservé** ; décision, mesures, autorité ; vérification des preuves (intactes, décision recalculée identique, attestation : aucune) | `OP-20261103T210711-73634e` | `edge_optimize_01…08` |
| 3. Régression | charge à écriture dominante lancée depuis l'interface → **Compensé** : coût pondéré ×2,1, critères `no_regression` et `weighted_workload_improved` en échec ; index retiré, absence confirmée dans le catalogue et dans les plans | `OP-20261103T210747-9dbb08` | `edge_regression_01…04` |
| 4. Incident | une exécution CLI tuée juste après son changement (code 86) : l'interface montre l'opération **Interrompue — reprise nécessaire** (pas « en cours »), 2 incidents bloquants (changement inachevé, opération inachevée) ; reprise manuelle confirmée par un dialogue ; « Reprise effectuée » ; fiche de l'exécution de reprise | `OP-20261103T210821-a5feca` | `edge_incident_01…05` |

Chaque parcours : 0 requête en erreur ; la seule erreur console (favicon absent du gabarit Vite)
a été corrigée avant l'exécution finale.

## 3. Les quatre parcours — application de bureau (Tauri)

Binaire réel `apps/desktop/src-tauri/target/release/desktop.exe` (reconstruit avec l'interface
finale), piloté par CDP (`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9333`), même
service, labo réinitialisé avant ; à la fin, seul l'arbre de processus de ce `desktop.exe` est fermé.
Exécution : `_qualification/db02/captures/journeys_tauri/` — **0 erreur console, 0 requête en échec**.

| Parcours | Résultat | Opération |
|---|---|---|
| 1. Premier lancement | jeton saisi dans la fenêtre native, Exploitation, réconciliation au démarrage | — |
| 2. Optimisation | lancée depuis l'application, **pause** au point sûr puis **reprise**, **Conservé** | `OP-20261103T211050-0a451e` |
| 3. Régression | **Compensé**, compensation confirmée | `OP-20261103T211123-9f6951` |
| 4. Incident | opération CLI interrompue détectée, reprise manuelle depuis l'application | — |

Après ces parcours, une dernière correction purement visuelle (marge au-dessus de la barre de
contexte collante) a été faite ; le binaire a été reconstruit avec elle et un parcours 1 réel y a été
rejoué (`_qualification/db02/captures/tauri_final_smoke/`).

## Captures sélectionnées (`docs/gui/db02/`)

| Fichier | Ce qu'il montre |
|---|---|
| `01_first_launch_token_gate.png` | premier lancement : saisie du jeton |
| `02_first_launch_exploitation.png` | Exploitation : cible vérifiée, bail, reprise automatique |
| `03_optimize_paused.png` | pause effective au point sûr, bail conservé, sémantique affichée |
| `04_optimize_measurements.png` | chaque échantillon avant/après, échelle log, contrôles stables |
| `05_optimize_evidence_verified.png` | intégrité, recalcul identique, attestation : aucune |
| `06_regression_decision.png` | critères en échec qui ont déclenché la compensation |
| `07_regression_compensation.png` | retrait prouvé, absence confirmée dans le catalogue et les plans |
| `08_incident_detected.png` | opération **interrompue**, 2 incidents bloquants, aucun contrôle trompeur (nom de l'ordinateur masqué) |
| `09_incident_recovered.png` | reprise manuelle effectuée depuis l'interface |
| `10_target_privilege_boundary.png` | identité épinglée/en direct, frontière lue dans les catalogues |
| `11_tauri_optimize_paused.png`, `12_tauri_incident_recovered.png` | les mêmes parcours dans l'application native (12 : nom de l'ordinateur masqué) |
| `13_1366x768_150pct_exploitation.png`, `14_2560x1440_100pct_sheet.png` | les deux extrêmes de la matrice |
| `15_overview_english.png` | l'interface en anglais |

## 4. Résolutions et mise à l'échelle

1366×768, 1920×1080, 2560×1440, chacune à 100 %, 125 % et 150 % (viewport CSS = résolution ÷
échelle, facteur de pixel = échelle) ; 4 écrans par combinaison (Exploitation, fiche, cible, liste) :
**36 captures, 0 débordement mesuré** (page, zone principale, panneaux, barre de contexte) —
`_qualification/db02/captures/matrix_final/matrix.json`. À 1366×768 et 150 % (911×512 CSS px), la
barre supérieure masque ses métriques secondaires du corpus, la barre de contexte passe sur deux
lignes, les tuiles en grille 3×2 ; tout reste lisible.

Limite déclarée : la mise à l'échelle est émulée par le navigateur (facteur de pixel), le réglage
d'affichage Windows n'a pas été modifié.

## 5. Accessibilité et langue

- Statuts toujours écrits et dotés d'un glyphe (jamais la couleur seule) ; contrastes AA calculés
  (`docs/gui/DESIGN_SYSTEM.md`) ; deux couleurs corrigées pendant le lot.
- Onglets de la fiche : `role=tab`, flèches gauche/droite ; dialogues de confirmation : rôle `dialog`,
  focus capturé, Échap annule, focus rendu ; régions nommées pour chaque panneau ; tableaux avec légende.
- FR/EN complets pour le poste (fichiers `ops.fr.json` / `ops.en.json`) ; le message journalisé par le
  moteur reste en anglais et est étiqueté comme tel.
- Testé : 59 tests de composants (dont lancement idempotent au double clic, dialogue d'annulation au
  clavier, onglets au clavier, statut écrit, déduplication du flux d'événements).

## 6. Sémantique affichée des contrôles

Pause et annulation : prises en compte au prochain point sûr, jamais au milieu d'un DDL ; la pause
conserve le bail ; l'annulation retire un changement déjà appliqué (compensation confirmée) ; une
annulation arrivée après la conservation est sans effet et la fiche le dit. Une opération interrompue
(processus disparu) n'offre aucun contrôle : l'API les refuse (`DB_OPERATION_INTERRUPTED`) et renvoie
vers la reprise. Une confirmation graphique ne remplace aucun contrôle côté service.
