# FABLE MASTER PROMPT

## FIELD HORIZON // COMMAND INTERFACE

### Client léger local, interface graphique complète et centre de contrôle épistémique

Tu interviens comme :

* architecte logiciel principal ;
* ingénieur Python senior ;
* ingénieur Rust/Tauri ;
* développeur React/TypeScript senior ;
* product designer spécialisé dans les interfaces denses ;
* ingénieur UX pour outils scientifiques et techniques ;
* spécialiste de visualisation de graphes ;
* responsable QA, packaging et documentation.

Ta mission n'est pas de produire une maquette décorative.

Tu dois **auditer, concevoir, implémenter, tester, documenter et livrer un véritable client graphique opérationnel pour Field Horizon**, exploitant autant que possible toutes les capacités réellement présentes dans le projet.

Le logiciel doit être utilisable quotidiennement comme centre de contrôle de Field Horizon.

Il doit être :

* local-first ;
* léger ;
* rapide ;
* observable ;
* traçable ;
* multiplateforme ;
* extensible ;
* compatible avec l'architecture actuelle ;
* utilisable sans connaissance du code ;
* suffisamment propre pour devenir ensuite le client de référence de Field Horizon ;
* suffisamment découplé pour qu'un futur client Godot puisse utiliser la même API.

---

# 1. OBJECTIF DU PRODUIT

Créer une application appelée :

# FIELD HORIZON // COMMAND INTERFACE

Nom technique recommandé :

```text
field-horizon-ui
```

Cette application doit transformer Field Horizon en un environnement de travail visuel permettant de :

* gérer les corpus ;
* importer et inspecter les sources ;
* lancer des recherches ;
* configurer le retrieval ;
* contrôler le Retrieval Planner v3 ;
* lancer et surveiller des cycles ;
* observer les agents et les étapes internes ;
* examiner les preuves retenues ;
* suivre la provenance ;
* explorer les liens sémantiques ;
* détecter et analyser les contradictions ;
* inspecter les états du canon ;
* comparer plusieurs exécutions ;
* consulter les métriques ;
* gérer les modèles locaux ;
* diagnostiquer le système ;
* exporter les résultats ;
* comprendre précisément pourquoi Field Horizon a produit une conclusion.

Ce ne doit pas être un simple chatbot posé devant Field Horizon.

Ce doit être **l'instrumentation visible de la machine**.

---

# 2. PRINCIPE FONDAMENTAL

L'interface ne doit jamais dissimuler le fonctionnement réel du système derrière une animation ou un texte pseudo-magique.

Chaque réponse importante doit pouvoir être reliée à :

* une requête ;
* une configuration ;
* un modèle ;
* un ensemble de sources ;
* des fragments de corpus ;
* des scores ;
* des liens sémantiques ;
* des contradictions ;
* une suite d'étapes ;
* une décision d'agent ;
* une évaluation ;
* un état du canon ;
* un horodatage ;
* un identifiant d'exécution.

Field Horizon doit paraître puissant parce qu'il est **observable**, et non parce que son interface agite du brouillard numérique devant l'utilisateur.

---

# 3. CONTRAINTES ABSOLUES

## 3.1 Préserver Field Horizon

Avant toute modification :

1. explorer le dépôt complet ;
2. lire le README et la documentation ;
3. inventorier les commandes CLI ;
4. inventorier les modules Python ;
5. inventorier les schémas SQLite ou PostgreSQL ;
6. inventorier les tests ;
7. inventorier les fichiers de configuration ;
8. identifier les fonctionnalités réellement terminées ;
9. identifier les fonctionnalités partielles ;
10. identifier les fonctionnalités seulement décrites dans la documentation.

Ne suppose rien.

Ne simule pas comme fonctionnel ce qui ne l'est pas.

Ne réécris pas le cœur de Field Horizon uniquement pour faciliter le frontend.

La CLI existante doit continuer à fonctionner.

Les comportements existants doivent rester compatibles.

L'interface et la CLI doivent utiliser, autant que possible, une même couche de services applicatifs afin d'éviter deux implémentations divergentes.

## 3.2 Pas de façade vide

Interdictions :

* boutons sans fonction ;
* pages remplies de fausses données ;
* métriques inventées ;
* graphes artificiels ;
* réponses statiques présentées comme générées ;
* endpoints fictifs laissés dans la version finale ;
* fonctions marquées « bientôt disponibles » sans explication ;
* écran de démonstration déconnecté du backend réel ;
* contrôles qui déclenchent uniquement un `console.log`;
* maquette Figma déguisée en application.

Les données de démonstration sont autorisées uniquement dans un mode explicitement nommé :

```text
DEMO MODE
```

Ce mode doit être visuellement impossible à confondre avec le fonctionnement réel.

## 3.3 Local-first

Le produit doit fonctionner sur la station locale sans dépendance obligatoire à un service distant.

Configuration par défaut :

```text
Frontend local
Backend Field Horizon local
Base SQLite locale
Ollama ou moteur LLM local
Aucune télémétrie externe
Aucun compte obligatoire
Aucun cloud obligatoire
```

Une future compatibilité cloud pourra être prévue par abstraction, mais elle ne doit pas dégrader le mode local.

## 3.4 Pas de Godot à ce stade

Godot n'est pas le client principal de cette phase.

Le cœur doit rester extérieur à Godot.

La future version Godot devra pouvoir consommer la même API que le client graphique.

## 3.5 Pas de monorepo monstrueux

Ne transforme pas l'ensemble des projets Pomegranate Interactive en un monorepo géant.

Le client Field Horizon reste dans le dépôt Field Horizon ou dans un dépôt clairement associé, avec des frontières propres.

---

# 4. AUDIT PRÉALABLE OBLIGATOIRE

Avant d'implémenter, produire :

```text
docs/gui/REPOSITORY_REALITY_AUDIT.md
```

Ce document doit contenir une matrice :

| Fonctionnalité | Présente | Partielle | Documentée seulement | Module | Commande CLI | Données | Exposable dans l'UI |
| -------------- | -------: | --------: | -------------------: | ------ | ------------ | ------- | ------------------: |

Inclure notamment, lorsqu'ils existent :

* ingestion de livres ;
* ingestion de corpus JSON ;
* recherche FTS ;
* recherche sémantique ;
* semantic profiles ;
* semantic links ;
* planner de retrieval ;
* modes `legacy`, `shadow`, `active` ;
* sélection de preuves ;
* scoring ;
* diversité ;
* provenance ;
* contradictions ;
* cycles ;
* agents ;
* synthèse ;
* évaluation ;
* critique ;
* rewrite ;
* canon ;
* états temporels du canon ;
* requêtes `as-of` ;
* lineage ;
* supply-chain report ;
* événements ;
* exports ;
* modèles locaux ;
* configuration ;
* diagnostics ;
* base SQLite ;
* éventuel adaptateur PostgreSQL/pgvector ;
* `PrincipalContext` ;
* politiques ou autorisations expérimentales.

Pour chaque fonction absente ou incomplète, l'interface doit :

* la masquer ;
* ou l'afficher comme expérimentale ;
* ou expliquer précisément sa disponibilité ;
* mais ne jamais prétendre qu'elle fonctionne.

Créer également :

```text
docs/gui/UI_FEATURE_COVERAGE.md
```

Ce fichier doit relier chaque fonctionnalité backend à son écran, son endpoint et ses tests.

---

# 5. AUDIT DE L'IDENTITÉ POMEGRANATE INTERACTIVE

Localiser le dépôt du site web Pomegranate Interactive.

Chercher notamment dans :

```text
~/Projects/
~/projects/
le dépôt Git courant
les dépôts Git voisins
les remotes Git configurés
```

Analyser réellement :

* les couleurs ;
* les variables CSS ;
* les polices ;
* les logos ;
* les icônes ;
* les textures ;
* les motifs ;
* les bordures ;
* les ombres ;
* les animations ;
* la navigation ;
* le traitement des titres ;
* le traitement des projets ;
* les composants réutilisables ;
* l'identité bilingue française et anglaise.

Réutiliser les véritables design tokens lorsque leur licence et leur structure le permettent.

Ne pas copier grossièrement des pages HTML dans l'application.

Transformer l'identité du site en un véritable **design system applicatif**.

Créer :

```text
apps/desktop/src/styles/tokens.css
apps/desktop/src/styles/typography.css
apps/desktop/src/styles/surfaces.css
apps/desktop/src/styles/motion.css
docs/gui/DESIGN_SYSTEM.md
```

Si le dépôt du site reste introuvable, utiliser la direction artistique de secours définie ci-dessous.

---

# 6. DIRECTION ARTISTIQUE

L'application doit ressembler à une pièce de l'univers Pomegranate Interactive.

Elle doit évoquer simultanément :

* une salle de contrôle enterrée ;
* un terminal UNIX ;
* un centre d'analyse militaire ;
* une archive interdite ;
* un scriptorium mécanisé ;
* une installation industrielle encore active après la disparition de ses opérateurs ;
* une machine scientifique qui aurait développé sa propre liturgie.

Elle ne doit pas ressembler à :

* une interface cyberpunk criarde ;
* un casino RGB ;
* un dashboard de cryptomonnaie ;
* une copie de Discord ;
* une copie de Notion ;
* une copie de ChatGPT ;
* une interface « glassmorphism » ;
* un produit SaaS générique ;
* une accumulation de cartes arrondies ;
* un terminal vert illisible utilisé comme décoration.

## 6.1 Palette

La palette exacte doit d'abord provenir du site Pomegranate Interactive.

À défaut, partir de ces familles :

```text
Obsidienne       : fonds principaux
Noir minéral     : surfaces profondes
Anthracite       : panneaux
Acier sombre     : séparateurs et composants techniques
Grenat           : identité Pomegranate
Rouge grenade    : sélection, danger, contradiction
Ivoire d'archive : textes principaux et documents
Cendre           : textes secondaires
Vert terminal    : succès, activité, données vivantes
Ambre sombre     : avertissement et état expérimental
```

Le grenat doit être une couleur identitaire, pas une nappe rouge omniprésente.

Le vert terminal doit signaler :

* activité ;
* exécution ;
* connexion ;
* disponibilité ;
* validation.

Le rouge doit signaler :

* contradiction ;
* rupture ;
* erreur ;
* verdict négatif ;
* donnée corrompue ;
* conflit de canon.

## 6.2 Typographie

Employer deux registres :

### Registre littéraire

Pour :

* titres ;
* manifestes ;
* noms de modules ;
* écrans de lancement ;
* citations ;
* synthèses longues.

### Registre machine

Police monospace pour :

* identifiants ;
* logs ;
* scores ;
* requêtes ;
* chemins ;
* événements ;
* horodatages ;
* fragments ;
* données structurées ;
* informations système.

Utiliser en priorité les polices déjà présentes dans le site.

Prévoir des fallbacks entièrement locaux.

## 6.3 Formes

* angles francs ;
* rayons faibles ;
* bordures fines ;
* grilles visibles ;
* séparateurs précis ;
* hiérarchie par contraste et densité ;
* blocs redimensionnables ;
* panneaux escamotables ;
* tableaux lisibles ;
* arbres et graphes manipulables.

Les contrôles critiques doivent avoir une silhouette distincte.

Le bouton de lancement d'un cycle ne doit pas ressembler à un banal bouton « Enregistrer ».

## 6.4 Texture

Employer avec retenue :

* grain minéral ;
* papier d'archive ;
* trame technique ;
* lignes de balayage presque invisibles ;
* motifs de circuits ;
* diagrammes ;
* coordonnées ;
* micro-marques d'impression ;
* numérotation de dossiers ;
* cachets de statut.

Aucun effet ne doit nuire à la lisibilité.

## 6.5 Mouvement

Les animations doivent expliquer un changement d'état.

Autorisé :

* apparition progressive des événements ;
* pulsation discrète d'un processus actif ;
* mouvement lent d'un flux dans un graphe ;
* ouverture mécanique d'un panneau ;
* transition de statut ;
* ligne de scan au démarrage ;
* changement subtil lors d'une contradiction.

Interdit :

* particules permanentes ;
* glitch incessant ;
* textes qui tremblent ;
* animations bloquant l'utilisateur ;
* boot sequence de dix secondes ;
* spectacle visuel à chaque clic.

## 6.6 Identité

Afficher proprement :

```text
FIELD HORIZON
COMMAND INTERFACE
POMEGRANATE INTERACTIVE
Copyright © Pomegranate Interactive 2026
```

Ne pas saturer chaque écran de logos.

---

# 7. LANGUES

L'application doit être bilingue :

```text
Français
English
```

Le français est la langue initiale recommandée.

Toutes les chaînes d'interface doivent passer par un système d'internationalisation.

Ne pas coder des textes directement dans les composants.

Les noms techniques réels du moteur peuvent rester en anglais lorsque leur traduction créerait de la confusion.

---

# 8. ARCHITECTURE TECHNIQUE CIBLE

## 8.1 Client desktop

Utiliser :

```text
Tauri 2
React
TypeScript
Vite
```

Utiliser les versions stables disponibles au moment de l'implémentation.

Ne pas utiliser Electron sauf impossibilité technique grave, démontrée et documentée.

## 8.2 Backend

Créer une couche API FastAPI autour du moteur existant.

Structure indicative, à adapter après audit :

```text
fieldhorizon/
├── api/
│   ├── app.py
│   ├── dependencies.py
│   ├── errors.py
│   ├── events.py
│   ├── schemas/
│   └── routers/
├── application/
│   ├── corpus_service.py
│   ├── cycle_service.py
│   ├── retrieval_service.py
│   ├── semantic_service.py
│   ├── provenance_service.py
│   ├── canon_service.py
│   └── system_service.py
└── ...
```

La CLI et l'API doivent appeler la même couche `application`.

Ne duplique pas la logique métier dans les routers FastAPI.

## 8.3 Communication

Utiliser :

* HTTP pour les opérations classiques ;
* SSE pour les événements continus unidirectionnels ;
* WebSocket uniquement lorsqu'une interaction bidirectionnelle réelle est nécessaire ;
* OpenAPI comme contrat ;
* types TypeScript générés depuis le contrat backend.

Chaque exécution doit recevoir un identifiant de corrélation.

Chaque événement doit au minimum contenir :

```json
{
  "event_id": "...",
  "run_id": "...",
  "cycle_id": "...",
  "timestamp": "...",
  "event_type": "...",
  "stage": "...",
  "severity": "...",
  "payload": {}
}
```

## 8.4 Gestion du backend local

Le client doit pouvoir :

1. se connecter à un backend Field Horizon déjà lancé ;
2. lancer le backend local comme processus géré ;
3. détecter sa disponibilité ;
4. afficher son état ;
5. redémarrer proprement le backend ;
6. reconnecter les flux après interruption ;
7. signaler les migrations nécessaires ;
8. ne jamais tuer arbitrairement un processus externe non lancé par lui.

Le binding par défaut doit être limité à :

```text
127.0.0.1
```

## 8.5 Données

Support principal :

```text
SQLite
```

Prévoir une abstraction propre pour :

```text
PostgreSQL
pgvector
```

Ne pas imposer PostgreSQL pour le client local.

## 8.6 Bibliothèques frontend

Utiliser avec discipline :

* TanStack Query pour l'état serveur ;
* React Flow pour les graphes interactifs ;
* Monaco Editor pour JSON, YAML, prompts et données structurées ;
* Apache ECharts pour les métriques et séries ;
* virtualisation pour les longues listes ;
* système de composants accessible ;
* store local minimal uniquement lorsque nécessaire.

Éviter un empilement de quinze bibliothèques se chevauchant.

---

# 9. STRUCTURE VISUELLE GLOBALE

L'interface principale doit comprendre :

## Barre supérieure

Afficher :

* nom du projet ou workspace ;
* backend connecté ou hors ligne ;
* base de données active ;
* modèle actif ;
* activité GPU ou moteur local si disponible ;
* planner mode ;
* nombre de cycles actifs ;
* langue ;
* accès à la palette de commandes.

## Navigation latérale

Sections proposées :

```text
COMMAND
CORPUS
RETRIEVAL
CYCLES
SEMANTICS
CANON
PROVENANCE
CONTRADICTIONS
EVALUATION
MODELS
SYSTEM
SETTINGS
```

Les sections non disponibles selon le backend doivent être masquées ou marquées expérimentales.

## Zone centrale

Espace principal adaptable à chaque module.

## Inspecteur droit

Panneau contextuel pour :

* métadonnées ;
* propriétés ;
* détails d'un nœud ;
* scores ;
* source ;
* tags ;
* relations ;
* actions secondaires.

## Console inférieure

Panneau redimensionnable contenant :

* événements ;
* logs ;
* erreurs ;
* appels ;
* métriques de runtime ;
* activité des agents ;
* changements d'état.

La console doit pouvoir être :

* ouverte ;
* réduite ;
* filtrée ;
* détachée ;
* vidée visuellement sans supprimer les journaux ;
* exportée.

---

# 10. ÉCRANS ET FONCTIONNALITÉS

## 10.1 Écran de démarrage

Créer un écran de démarrage bref et fonctionnel.

Il doit montrer les étapes réelles :

```text
Locating configuration
Opening database
Checking schema
Connecting model provider
Loading capabilities
Starting event channel
Ready
```

Une erreur doit être immédiatement exploitable.

Exemples :

* base introuvable ;
* schéma trop ancien ;
* port occupé ;
* modèle absent ;
* Ollama indisponible ;
* corpus vide ;
* permission refusée.

Chaque erreur doit proposer une action concrète et sûre.

## 10.2 COMMAND

Le tableau de bord principal doit afficher :

* santé globale ;
* backend ;
* base active ;
* version ;
* modèle actif ;
* nombre de sources ;
* nombre de chunks ;
* entrées JSON ;
* profils sémantiques ;
* liens sémantiques ;
* nombre de cycles ;
* derniers cycles ;
* contradictions récentes ;
* planner mode ;
* files d'exécution ;
* erreurs récentes ;
* activité système ;
* événements récents.

Prévoir une visualisation discrète du pipeline :

```text
INTERPRETER
    ↓
RETRIEVAL
    ↓
AGENTS
    ↓
SYNTHESIS
    ↓
EVALUATION
    ↓
CRITIC / REWRITE
    ↓
CANON
```

Les étapes doivent refléter la configuration réelle.

## 10.3 CORPUS

Fonctions :

* lister les sources ;
* rechercher ;
* filtrer ;
* trier ;
* importer un fichier ;
* importer un dossier lorsque supporté ;
* prévisualiser ;
* choisir le type de source ;
* définir les métadonnées ;
* lancer l'ingestion ;
* suivre la progression ;
* afficher les erreurs ;
* réindexer ;
* désactiver une source ;
* supprimer une source avec confirmation explicite ;
* visualiser les chunks ;
* examiner les entrées JSON ;
* afficher les liens dérivés ;
* détecter les doublons ;
* afficher les statistiques d'ingestion.

Types de sources à exposer uniquement s'ils existent réellement :

* texte ;
* Markdown ;
* JSON ;
* YAML ;
* PDF ;
* corpus structuré ;
* axiomes ;
* livres ;
* documents de canon.

La fiche source doit contenir :

* identifiant ;
* titre ;
* origine ;
* chemin ;
* hash ;
* date d'import ;
* version ;
* langue ;
* taille ;
* nombre de chunks ;
* statut ;
* tags ;
* relations ;
* erreurs ;
* provenance.

## 10.4 RETRIEVAL

Créer un véritable laboratoire de retrieval.

Fonctions :

* champ de requête ;
* recherche rapide ;
* recherche avancée ;
* filtres de source ;
* filtres temporels ;
* filtres sémantiques ;
* nombre de résultats ;
* seuils ;
* pondérations ;
* diversité ;
* méthode de ranking ;
* sélection du planner ;
* explication du retrieval ;
* sauvegarde de presets ;
* comparaison de deux configurations.

Chaque résultat doit afficher :

* texte ;
* score ;
* rang ;
* source ;
* chunk ;
* méthode de sélection ;
* raison de sélection ;
* profil sémantique ;
* liens ;
* provenance ;
* contradictions éventuelles ;
* accès au document parent.

## 10.5 RETRIEVAL PLANNER V3

Lorsque le planner v3 existe, intégrer les trois modes :

```text
legacy
shadow
active
```

### Legacy

Le système utilise le retrieval historique.

### Shadow

Le système produit sa réponse avec le retrieval historique, mais exécute le planner v3 en parallèle.

L'interface doit comparer :

* preuves retenues ;
* preuves rejetées ;
* scores ;
* classement ;
* diversité ;
* provenance ;
* couverture ;
* contradictions ;
* verdict potentiel ;
* latence ;
* consommation de contexte.

Créer un écran de comparaison visuelle avec :

* colonnes synchronisées ;
* éléments communs ;
* éléments uniquement legacy ;
* éléments uniquement planner v3 ;
* variations de scores ;
* causes des divergences ;
* verdict de régression.

### Active

Le planner v3 devient la voie principale.

Le passage en mode `active` doit être clairement visible.

Le changement de mode doit être enregistré dans les événements.

L'interface ne doit pas activer ce mode par accident.

## 10.6 CYCLES

Créer un éditeur de lancement de cycle.

Paramètres possibles, selon les capacités réelles :

* requête ou intention ;
* workspace ;
* corpus ;
* sources ;
* modèle ;
* profil d'agent ;
* planner mode ;
* stratégie de retrieval ;
* budget de contexte ;
* nombre maximum d'étapes ;
* seuils ;
* seed ;
* mode reproductible ;
* politique ;
* contexte principal ;
* tags ;
* notes.

Fonctions :

* lancer ;
* suspendre si supporté ;
* annuler ;
* relancer ;
* dupliquer ;
* comparer ;
* exporter ;
* ouvrir les preuves ;
* ouvrir la lineage ;
* inspecter les erreurs.

Ne jamais afficher un bouton « Pause » si le backend ne sait pas réellement suspendre un cycle.

## 10.7 LIVE CYCLE

Pendant un cycle, présenter une chronologie vivante.

Panneaux :

### Timeline

Événements dans l'ordre réel.

### Retrieval

Preuves en cours de sélection.

### Agents

Agents actifs, état, tâche, durée, résultat.

### Synthesis

Construction de la réponse.

### Evaluation

Scores, critères, verdicts.

### Critic / Rewrite

Critiques et révisions.

### Canon

Changements proposés ou appliqués.

### Runtime

* durée ;
* tokens si disponibles ;
* contexte ;
* modèle ;
* mémoire ;
* erreurs ;
* latence ;
* activité matérielle disponible ;
* nombre d'appels.

Chaque étape doit pouvoir être ouverte dans l'inspecteur.

## 10.8 SEMANTICS

Créer un explorateur des profils et liens sémantiques.

Vues :

* graphe ;
* tableau ;
* liste ;
* matrice ;
* voisinage d'une entité ;
* chemin entre deux entités.

Le graphe doit permettre :

* zoom ;
* pan ;
* sélection ;
* recherche ;
* filtrage ;
* regroupement ;
* coloration par type ;
* épaisseur par poids ;
* affichage des directions ;
* repli des groupes ;
* expansion à la demande ;
* mini-carte ;
* recentrage ;
* export SVG ou PNG ;
* accès aux données sources.

Ne pas charger cent mille nœuds d'un coup comme un animal sans défense face à la complexité.

Utiliser :

* agrégation ;
* pagination ;
* limites ;
* expansion progressive ;
* calcul serveur ;
* avertissements de densité.

## 10.9 CANON

Créer un espace de consultation du canon.

Fonctions selon l'implémentation réelle :

* lister les états ;
* consulter une entrée ;
* comparer deux versions ;
* afficher les changements ;
* afficher l'auteur ou le cycle ;
* remonter aux preuves ;
* afficher les relations ;
* consulter l'état à une date donnée ;
* requêtes `as-of` ;
* visualiser les branches ;
* exporter un snapshot ;
* examiner les changements proposés ;
* accepter ou rejeter uniquement si ce workflow existe réellement.

Les états temporels doivent être présentés comme une chronologie inspectable.

## 10.10 PROVENANCE

Créer un explorateur de lineage.

À partir de n'importe quel résultat, permettre de remonter vers :

```text
Résultat
→ Synthèse
→ Décisions
→ Preuves
→ Chunks
→ Sources
→ Axiomes
```

Afficher :

* arêtes de provenance ;
* type de transformation ;
* identifiant ;
* horodatage ;
* version ;
* score ;
* acteur ou agent ;
* hash ;
* statut ;
* rupture éventuelle de chaîne.

Créer un mode :

```text
SUPPLY-CHAIN REPORT
```

Il doit produire un rapport lisible, exportable et reproductible.

## 10.11 CONTRADICTIONS

Créer une interface dédiée aux contradictions.

Fonctions :

* liste des contradictions ;
* gravité ;
* état ;
* sources concernées ;
* fragments opposés ;
* type de contradiction ;
* date de détection ;
* cycle ;
* résolution ;
* notes ;
* filtres ;
* comparaison côte à côte ;
* accès à la lineage ;
* accès au canon.

Différencier visuellement :

* contradiction directe ;
* tension ;
* ambiguïté ;
* divergence temporelle ;
* incompatibilité de provenance ;
* donnée probablement obsolète ;
* faux positif.

Ne jamais réduire toutes les divergences à un simple badge rouge.

## 10.12 EVALUATION

Créer un laboratoire d'évaluation et de régression.

Fonctions :

* jeux de tests ;
* scénarios ;
* exécutions ;
* scores ;
* comparaison de modèles ;
* comparaison de planners ;
* comparaison de prompts ;
* latence ;
* stabilité ;
* couverture ;
* diversité ;
* fidélité aux sources ;
* taux de contradiction ;
* dérive ;
* historique.

Prévoir des graphiques sobres et lisibles.

Créer un écran de validation du planner v3 permettant de comparer statistiquement :

```text
legacy
shadow
active candidate
```

Aucune bascule automatique vers `active` ne doit être décidée sur la base d'une seule exécution.

## 10.13 MODELS

Créer un panneau pour les fournisseurs et modèles disponibles.

Selon les adaptateurs réellement présents :

* Ollama ;
* vLLM ;
* OpenAI-compatible ;
* autres fournisseurs déjà implémentés.

Afficher :

* fournisseur ;
* modèle ;
* disponibilité ;
* contexte ;
* endpoint ;
* statut ;
* temps de réponse ;
* configuration ;
* modèle par défaut ;
* modèle utilisé par rôle ;
* erreur de connexion ;
* dernière vérification.

Ne jamais exposer les secrets dans l'interface ou les journaux.

## 10.14 SYSTEM

Créer un écran de diagnostic.

Afficher :

* version Field Horizon ;
* version UI ;
* version Python ;
* version API ;
* version du schéma ;
* chemin de la base ;
* taille de la base ;
* état FTS ;
* état des migrations ;
* processus backend ;
* uptime ;
* files d'exécution ;
* espace disque ;
* mémoire ;
* GPU si détectable ;
* moteur LLM ;
* ports ;
* logs ;
* configuration chargée ;
* capacités annoncées.

Ajouter des diagnostics exécutables :

* health check ;
* test DB ;
* test FTS ;
* test modèle ;
* test API ;
* test ingestion ;
* test retrieval ;
* test événement ;
* test écriture dans le dossier de sortie.

Les diagnostics destructifs sont interdits.

## 10.15 SETTINGS

Catégories :

* apparence ;
* langue ;
* backend ;
* base ;
* modèles ;
* corpus ;
* retrieval ;
* cycles ;
* journaux ;
* exports ;
* confidentialité ;
* fonctions expérimentales.

Ajouter :

* import de configuration ;
* export de configuration ;
* validation ;
* comparaison avec les valeurs par défaut ;
* restauration ciblée ;
* aperçu des changements.

Ne jamais écraser silencieusement un fichier de configuration.

---

# 11. PALETTE DE COMMANDES

Ajouter une palette accessible au clavier.

Exemples :

```text
Open source
Run cycle
Search corpus
Open latest cycle
Switch planner mode
Show contradictions
Open lineage
Restart managed backend
Export current view
Toggle console
Change language
```

Chaque commande doit être recherchable.

Prévoir des raccourcis cohérents.

Les raccourcis destructifs doivent exiger une confirmation.

---

# 12. RECHERCHE GLOBALE

Ajouter une recherche globale capable de retrouver :

* sources ;
* chunks ;
* cycles ;
* résultats ;
* événements ;
* profils sémantiques ;
* liens ;
* contradictions ;
* états du canon ;
* modèles ;
* paramètres.

Les résultats doivent être groupés par type.

La recherche globale ne doit pas devenir une deuxième implémentation incohérente du moteur de retrieval.

---

# 13. OBSERVABILITÉ

L'interface doit rendre visibles :

* l'état de chaque tâche ;
* sa progression ;
* son temps d'exécution ;
* son identifiant ;
* sa cause ;
* ses entrées ;
* ses sorties ;
* ses erreurs ;
* ses dépendances ;
* les événements associés.

Créer un modèle d'état commun :

```text
idle
queued
starting
running
waiting
completed
failed
cancelled
degraded
```

Les erreurs backend doivent être transformées en messages structurés :

```json
{
  "code": "FH_RETRIEVAL_INDEX_UNAVAILABLE",
  "title": "Index de recherche indisponible",
  "detail": "...",
  "remediation": "...",
  "correlation_id": "..."
}
```

Ne pas afficher une trace Python brute à l'utilisateur par défaut.

Permettre toutefois de consulter la trace complète dans les détails techniques.

---

# 14. SÉCURITÉ LOCALE

Appliquer le principe du moindre privilège.

Exigences :

* backend bindé sur localhost par défaut ;
* CORS limité ;
* jeton de session local si nécessaire ;
* aucune télémétrie ;
* aucune commande shell arbitraire depuis le frontend ;
* accès aux fichiers limité aux chemins explicitement sélectionnés ;
* validation stricte des chemins ;
* protection contre les traversées de répertoires ;
* secrets absents des logs ;
* capacités Tauri minimales ;
* allowlists explicites ;
* confirmations pour les suppressions ;
* sauvegarde avant migration sensible ;
* journalisation des actions destructives.

Documenter le modèle de menace dans :

```text
docs/gui/SECURITY_MODEL.md
```

---

# 15. PERFORMANCE

Le client doit rester fluide avec :

* plusieurs milliers de sources ;
* plusieurs dizaines de milliers de chunks ;
* de longues timelines ;
* de nombreux événements ;
* de grands graphes.

Employer :

* pagination serveur ;
* virtualisation ;
* chargement progressif ;
* cache contrôlé ;
* requêtes annulables ;
* debounce ;
* rendu différé ;
* agrégation des graphes ;
* lazy loading des écrans ;
* streaming des événements.

Ne pas transférer l'intégralité de la base au frontend pour afficher un tableau.

---

# 16. ACCESSIBILITÉ

Exigences minimales :

* navigation clavier ;
* focus visible ;
* contrastes suffisants ;
* textes redimensionnables ;
* labels accessibles ;
* alternatives aux informations purement colorées ;
* support de `prefers-reduced-motion` ;
* tableaux exploitables ;
* annonces d'état importantes ;
* raccourcis documentés.

La densité visuelle ne doit pas devenir une excuse pour rendre l'interface inutilisable.

---

# 17. GESTION DES ÉTATS

Chaque écran et composant relié au backend doit gérer :

* chargement ;
* succès ;
* absence de données ;
* erreur ;
* perte de connexion ;
* permission insuffisante ;
* fonction indisponible ;
* fonction expérimentale ;
* données partielles ;
* données obsolètes ;
* reconnexion.

Aucune page blanche.

Aucun spinner éternel.

Aucun bouton qui reste actif après une erreur irréversible.

---

# 18. EXPORTS

Permettre, selon le contenu :

* JSON ;
* Markdown ;
* CSV ;
* HTML ;
* SVG ;
* PNG ;
* bundle reproductible.

Un bundle de cycle doit pouvoir inclure :

```text
configuration
requête
identifiants
modèle
preuves
scores
événements
résultat
évaluation
provenance
contradictions
état du canon
versions logicielles
```

Les exports doivent mentionner :

```text
Generated by Field Horizon
Pomegranate Interactive
Copyright © Pomegranate Interactive 2026
```

---

# 19. MODE EXPÉRIMENTAL ET EMERGENCE MONITOR

Si le dépôt contient réellement des notions comme :

* Basilisk ;
* emergence ;
* anomaly ;
* recursion ;
* attractor ;
* rupture sémantique ;
* détection d'entité ;

créer une section expérimentale nommée :

```text
EMERGENCE MONITOR
```

Cette section ne doit jamais prétendre qu'un événement surnaturel ou conscient s'est produit.

Elle doit visualiser des signaux mesurables :

* anomalies ;
* récurrences ;
* convergence sémantique ;
* motifs ;
* densité de liens ;
* boucles ;
* contradictions inhabituelles ;
* changement de comportement ;
* événements rares.

Le système doit **détecter** ces états selon des critères documentés.

Il ne doit pas produire un événement artificiel uniquement parce que l'utilisateur a appuyé sur un bouton nommé « Manifest ».

L'esthétique peut être inquiétante.

La logique doit rester rigoureuse.

Si ces fonctions n'existent pas réellement, créer uniquement une spécification documentée et ne pas les présenter comme disponibles.

---

# 20. PREMIER LANCEMENT

Prévoir un assistant de premier lancement capable de :

1. détecter le dépôt Field Horizon ;
2. détecter l'environnement Python ;
3. détecter la configuration ;
4. détecter la base ;
5. vérifier le schéma ;
6. vérifier le moteur LLM ;
7. proposer un backend existant ou géré ;
8. effectuer un health check ;
9. ouvrir le dashboard.

Chemin connu probable :

```text
~/Projects/FieldHorizon
```

Ne pas coder ce chemin comme unique possibilité.

Permettre :

* détection automatique ;
* variable d'environnement ;
* argument de lancement ;
* sélection manuelle.

---

# 21. COMMANDES D'EXÉCUTION

Livrer au minimum :

```bash
./scripts/gui-dev.sh
./scripts/gui-build.sh
./scripts/gui-test.sh
./scripts/gui-package.sh
```

Ajouter une commande simple de lancement :

```bash
./scripts/field-horizon-ui
```

Ou une commande équivalente réellement fonctionnelle.

Le mode développement doit lancer :

* backend ;
* frontend ;
* flux de logs ;
* vérifications préalables.

Le script doit :

* détecter les dépendances ;
* éviter les installations destructives ;
* afficher les erreurs clairement ;
* nettoyer correctement les processus lancés ;
* fonctionner depuis la racine du dépôt.

---

# 22. PACKAGING

Priorités :

1. Ubuntu/Linux ;
2. Windows ;
3. macOS si l'infrastructure le permet.

Produire autant que possible :

```text
AppImage
.deb
Windows installer
```

L'application doit pouvoir utiliser un backend Field Horizon externe ou embarqué comme sidecar géré.

Documenter exactement ce qui est inclus dans chaque package.

Ne pas embarquer arbitrairement des modèles LLM de plusieurs gigaoctets dans l'installateur.

---

# 23. STRUCTURE CIBLE INDICATIVE

Adapter cette structure à la réalité du dépôt :

```text
FieldHorizon/
├── apps/
│   └── desktop/
│       ├── src/
│       │   ├── api/
│       │   ├── app/
│       │   ├── components/
│       │   ├── features/
│       │   ├── hooks/
│       │   ├── i18n/
│       │   ├── layouts/
│       │   ├── routes/
│       │   ├── styles/
│       │   ├── types/
│       │   └── utils/
│       ├── src-tauri/
│       ├── tests/
│       └── package.json
├── fieldhorizon/
│   ├── api/
│   ├── application/
│   └── ...
├── docs/
│   └── gui/
├── scripts/
│   ├── gui-dev.sh
│   ├── gui-build.sh
│   ├── gui-test.sh
│   ├── gui-package.sh
│   └── field-horizon-ui
└── tests/
    ├── api/
    └── integration/
```

Ne déplace pas massivement les modules existants sans raison.

---

# 24. TESTS

## Backend

Ajouter :

* tests unitaires des services ;
* tests des routers ;
* tests des schémas ;
* tests d'erreur ;
* tests d'événements ;
* tests de compatibilité CLI/API ;
* tests avec base temporaire ;
* tests de migration.

## Frontend

Ajouter :

* tests de composants critiques ;
* tests des états ;
* tests des formulaires ;
* tests des erreurs ;
* tests de navigation ;
* tests des permissions ;
* tests des graphes avec petits jeux de données.

## End-to-end

Scénarios obligatoires :

1. démarrer l'application ;
2. connecter le backend ;
3. ouvrir une base ;
4. consulter les sources ;
5. importer une source de test ;
6. lancer une recherche ;
7. inspecter une preuve ;
8. lancer un cycle ;
9. suivre ses événements ;
10. consulter son résultat ;
11. ouvrir sa provenance ;
12. exporter le cycle ;
13. redémarrer l'application ;
14. retrouver l'historique.

Ajouter également :

* perte du backend pendant un cycle ;
* base inaccessible ;
* modèle absent ;
* erreur d'ingestion ;
* résultat vide ;
* timeline longue ;
* grand graphe ;
* annulation d'une requête.

---

# 25. CRITÈRES D'ACCEPTATION

La livraison ne peut être considérée comme terminée que si :

* la CLI existante fonctionne toujours ;
* le client démarre avec une commande documentée ;
* le backend local est détecté ou lancé proprement ;
* l'état du système est visible ;
* les sources réelles sont consultables ;
* une recherche réelle peut être exécutée ;
* un cycle réel peut être lancé ;
* les événements sont visibles en direct ;
* le résultat peut être inspecté ;
* les preuves peuvent être ouvertes ;
* la provenance peut être suivie ;
* les erreurs sont compréhensibles ;
* les données ne sont pas simulées ;
* la langue française fonctionne ;
* la langue anglaise fonctionne ;
* l'application est utilisable au clavier ;
* l'interface reste lisible en 1366×768 ;
* l'interface exploite correctement un écran 2560×1440 ;
* le packaging Linux fonctionne ;
* les tests essentiels passent ;
* la documentation permet à une autre personne de lancer le projet ;
* les fonctions absentes sont honnêtement identifiées ;
* aucun secret n'est commité ;
* aucune télémétrie externe n'est introduite.

---

# 26. QUALITÉ VISUELLE ATTENDUE

Le résultat final doit donner l'impression que Field Horizon dispose enfin de son véritable instrument de commandement.

Il doit être :

* sombre sans être opaque ;
* dense sans être confus ;
* théâtral sans être kitsch ;
* technique sans être stérile ;
* inquiétant sans devenir adolescent ;
* littéraire sans sacrifier la précision ;
* moderne sans ressembler à tous les produits IA de l'année ;
* parfaitement cohérent avec Pomegranate Interactive.

La première impression doit être :

> Cette machine sait exactement ce qu'elle fait, et elle conserve les preuves.

---

# 27. PROTOCOLE D'EXÉCUTION

Procéder dans cet ordre.

## Phase 1 — Réalité

* audit du dépôt ;
* audit du site ;
* inventaire fonctionnel ;
* inventaire des risques ;
* matrice de couverture.

## Phase 2 — Fondation

* branche Git dédiée ;
* couche de services ;
* API FastAPI ;
* endpoint de capacités ;
* endpoint de santé ;
* modèle d'événements ;
* tests backend.

## Phase 3 — Shell

* projet Tauri ;
* React/TypeScript/Vite ;
* design tokens ;
* navigation ;
* layout ;
* connexion backend ;
* i18n ;
* gestion globale des erreurs.

## Phase 4 — Workflows essentiels

* dashboard ;
* corpus ;
* retrieval ;
* lancement de cycle ;
* cycle live ;
* résultat ;
* provenance.

## Phase 5 — Fonctions avancées

* planner v3 ;
* semantic graph ;
* canon ;
* contradictions ;
* évaluation ;
* modèles ;
* diagnostics.

## Phase 6 — Durcissement

* tests E2E ;
* sécurité ;
* performance ;
* accessibilité ;
* reconnexion ;
* exports ;
* packaging.

## Phase 7 — Livraison

* documentation ;
* captures d'écran ;
* rapport de couverture ;
* commandes exactes ;
* limitations restantes ;
* commits propres.

Ne t'arrête pas après avoir produit une architecture ou un squelette.

Continue jusqu'à obtenir une application réellement lançable.

---

# 28. DISCIPLINE GIT

Créer une branche dédiée, par exemple :

```text
feature/field-horizon-command-interface
```

Effectuer des commits logiques :

```text
docs: audit Field Horizon GUI capabilities
feat(api): add local application API
feat(events): expose cycle event stream
feat(ui): add desktop application shell
feat(corpus): implement source explorer
feat(retrieval): implement retrieval laboratory
feat(cycles): implement live cycle console
feat(provenance): add lineage explorer
feat(gui): apply Pomegranate design system
test(gui): add end-to-end workflows
docs(gui): add runbook and packaging guide
```

Ne pas accumuler l'ensemble du travail dans un unique commit informe.

Ne pas pousser ou fusionner sur la branche principale sans instruction explicite.

---

# 29. DOCUMENTATION À LIVRER

Créer :

```text
docs/gui/REPOSITORY_REALITY_AUDIT.md
docs/gui/ARCHITECTURE.md
docs/gui/API_CONTRACT.md
docs/gui/DESIGN_SYSTEM.md
docs/gui/UI_FEATURE_COVERAGE.md
docs/gui/SECURITY_MODEL.md
docs/gui/TEST_STRATEGY.md
docs/gui/PACKAGING.md
docs/gui/RUNBOOK.md
docs/gui/TROUBLESHOOTING.md
```

Le `RUNBOOK.md` doit contenir les commandes exactes pour :

* installer les dépendances ;
* lancer en développement ;
* lancer les tests ;
* construire ;
* packager ;
* connecter un backend existant ;
* changer de base ;
* configurer Ollama ;
* diagnostiquer une panne ;
* lire les logs.

---

# 30. RAPPORT FINAL

À la fin, fournir un rapport contenant :

## Résumé

Ce qui a réellement été construit.

## Audit

Fonctions trouvées dans Field Horizon.

## Couverture

Fonctions accessibles depuis l'interface.

## Architecture

Décisions importantes et raisons.

## Fichiers

Principaux fichiers créés ou modifiés.

## Commandes

Commandes exactes de lancement, test et build.

## Tests

Tests exécutés et résultats.

## Packaging

Artefacts générés.

## Captures

Captures des écrans essentiels.

## Limites

Fonctions absentes, partielles ou expérimentales.

## Prochaines étapes

Travaux restant réellement nécessaires, classés par priorité.

Ne jamais annoncer « production ready » si les critères ne sont pas satisfaits.

---

# 31. DERNIÈRE DIRECTIVE

Ne construis pas une peau graphique autour d'un terminal.

Ne construis pas un chatbot.

Ne construis pas un prototype jetable.

Construis le **poste de commandement complet de Field Horizon** :

* un lieu où les corpus deviennent navigables ;
* où les cycles deviennent observables ;
* où les agents cessent d'être invisibles ;
* où les preuves peuvent être interrogées ;
* où les contradictions sont exposées ;
* où le canon peut être parcouru dans le temps ;
* où chaque verdict conserve la mémoire de ses conditions de production.

Le résultat doit pouvoir devenir l'une des pièces centrales de Pomegranate Interactive.

Commence immédiatement par l'audit réel du dépôt et du site, puis poursuis l'implémentation jusqu'à obtenir un client fonctionnel, testé, documenté et lançable.
