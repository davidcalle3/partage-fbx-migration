# Partage · Migration Freebox

Outil temporaire, **indépendant** du site Partage et de son encodeur : navigation Freebox OS en lecture seule, sélection de fichiers/dossiers, prévisualisation de la taille, copie directe Freebox → VPS (en mémoire, par blocs de 8 Mio) → S3 OVH, suivi et reprise des transferts.

**La source Freebox n'est jamais modifiée.** La seule écriture dans Partage est la création des dossiers sélectionnés et la publication des fichiers **après** vérification de leur présence et de leur taille dans S3. Aucun fichier média complet n'est conservé sur le VPS. Les vidéos importées suivent ensuite le traitement vidéo normal de Partage.

## Accès direct HTTPS, sans tunnel SSH (recommandé)

L'interface est désormais protégée par une **authentification HTTP Basic dédiée**
avec mot de passe aléatoire et hash PBKDF2 conservé dans le répertoire privé
`~/partage-freebox-migration`. **Ne jamais exposer directement le port HTTP 8791
sur Internet** : les identifiants HTTP Basic doivent obligatoirement transiter
sur une connexion HTTPS valide.

Le service Uvicorn reste lié à `127.0.0.1:8791`. Le reverse proxy HTTPS qui
sert déjà Partage publie seulement le chemin `/fbx-migration/`.
Les mêmes URL fonctionnent sur mobile et ordinateur, sans tunnel SSH.

### 1. Mettre à jour l'outil et créer le mot de passe privé

Sur le VPS :

```sh
cd ~/partage-fbx-migration
git pull --ff-only
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m http_auth --create
```

**Enregistrer le mot de passe affiché dans un gestionnaire** : il n'est pas
possible de récupérer sa valeur depuis le fichier de hash. Le programme
refuse de remplacer un mot de passe existant et refuse de démarrer sans
un fichier d'identifiants correct et protégé par chmod 600.
Aucun identifiant n'est écrit dans GitHub ni dans le `.env` de Partage.

Si l'application tournait avant la mise à jour, la redémarrer **après**
avoir créé le mot de passe :

```sh
systemctl --user restart partage-fbx-migration.service
systemctl --user status partage-fbx-migration.service --no-pager
```

Si elle tourne dans un terminal, arrêter ce processus avec Ctrl+C, puis
relancer la commande Uvicorn habituelle. **Ne pas lancer deux instances**.

### 2. Connecter le chemin à l'HTTPS existant

**Ton VPS utilise Nginx**, avec le site Partage dans
`/etc/nginx/sites-available/partage`. Une commande idempotente permet
d'ajouter la route automatiquement, **après** vérification que le service
répond HTTP 401 avec un défi d'authentification, pour ne jamais l'exposer
accidentellement sans mot de passe :

```sh
sudo python3 ~/partage-fbx-migration/deploy/manage_nginx.py --apply
```

L'installateur cible uniquement l'unique bloc HTTPS du site, enregistre
une sauvegarde `partage.pre-fbx-...`, valide la configuration Nginx, puis
la recharge. En cas d'échec de validation ou de rechargement, il remet
la configuration d'origine. Il refuse d'intervenir si l'application
n'est pas protégée, ou si le site comporte plusieurs blocs HTTPS ambigus.

Pour retirer la route après migration :

```sh
sudo python3 ~/partage-fbx-migration/deploy/manage_nginx.py --remove
```

**Modification manuelle (alternative)** : ajouter les deux blocs `location` de
`deploy/nginx-https-location.conf` **à l'intérieur du bloc server qui écoute
déjà sur 443 pour Partage**, sans modifier le bloc sur 80 ni les certificats.
La directive `proxy_pass http://127.0.0.1:8791/;`, avec son slash final,
retire le préfixe du chemin. Ne pas utiliser un vhost séparé non protégé.

```sh
sudo nginx -t && sudo systemctl reload nginx
```

**Caddy** : ajouter les directives de `deploy/caddy-https-site-block.caddy`
au bloc du site HTTPS Partage existant, avant son éventuel
`handle` générique. Ne pas remplacer tout le Caddyfile.

```sh
sudo caddy validate --config /etc/caddy/Caddyfile &&
sudo systemctl reload caddy
```

Après validation, ouvrir **https://DOMAINE_OU_IP_EXISTANT_DE_PARTAGE/fbx-migration/**.
Le navigateur demande l'identifiant `migration` et le mot de passe privé
généré à l'étape 1. Un HTTPS existant avec certificat non approuvé continuera
à générer un avertissement de certificat ; cette configuration ne le corrige pas.

**Important :** un accès SSH ponctuel reste utile pour administrer le VPS,
mais **aucun tunnel SSH permanent n'est nécessaire pour utiliser l'interface**.
Ne pas ouvrir le port 8791 au firewall ni changer `--host 127.0.0.1`.

### Désactivation de l'accès public après la migration

Retirer uniquement les blocs ajoutés au vhost Nginx ou Caddy, revalider la
configuration, recharger le proxy, puis arrêter le service utilisateur.
Ne pas modifier les originaux Freebox, ni le `.env`, ni les volumes Partage.

## Installation sur le VPS

Dépôt déjà cloné dans `/home/ubuntu/partage-fbx-migration`. Les certificats Freebox et l'autorisation sont **déjà présents** dans `/home/ubuntu/partage-freebox-migration`. Ne copiez pas ces secrets dans le dépôt GitHub.

1. Récupérer le code (sans réinitialiser les fichiers locaux) :

   ```sh
   cd ~/partage-fbx-migration
   git status --short
   git pull --ff-only
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python -m unittest discover -s tests -v
   ```

2. Créer un fichier **local** avec l'adresse HTTPS et l'UID de votre Freebox. Ce ne sont pas des secrets d'authentification, mais ce fichier est quand même conservé hors du dépôt public :

   ```sh
   cat > ~/partage-freebox-migration/config.json <<'EOF'
   {
     "host": "https://ADRESSE_FREEBOX:PORT",
     "uid": "UID_RETOURNE_PAR_API_VERSION"
   }
   EOF
   chmod 600 ~/partage-freebox-migration/config.json
   chmod 600 ~/partage-freebox-migration/authorization.json
   ```

3. Identifier le chemin du fichier d'environnement **existant** de Partage. **Ne pas le modifier, déplacer ni régénérer** :

   ```sh
   for f in /opt/partage/.env /opt/partage/app/.env; do
      [ ! -f "$f" ] || echo "Fichier Partage existant : $f"
   done
   ```

4. Démarrer l'outil, depuis un terminal SSH dédié. Adapter uniquement le chemin de `PARTAGE_ENV_FILE` si nécessaire :

   ```sh
   cd ~/partage-fbx-migration
   PARTAGE_ENV_FILE=/opt/partage/.env \
     .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8791 --workers 1
   ```

   Au démarrage, le programme vérifie le certificat, l'UID Freebox, les droits Explorateur, le schéma SQLite Partage, les identifiants S3 et l'accès au verrou d'unique worker. **Il ne modifie ni le `.env` ni la configuration Docker**. Les transferts reprendront à la prochaine exécution si le programme s'arrête.

5. Alternative facultative (accès local sans reverse proxy) : dans un second terminal sur votre ordinateur, ouvrir le tunnel SSH :

   ```sh
   ssh -N -L 8791:127.0.0.1:8791 ubuntu@IP_DU_VPS
   ```

   Puis ouvrir **http://127.0.0.1:8791** dans votre navigateur (l'authentification dédiée reste exigée). Le port **8791 n'est pas exposé publiquement**. L'accès direct par le proxy HTTPS existant est décrit plus haut ; il nécessite l'authentification dédiée. Ne jamais exposer le service en HTTP public.

Pour les copies de plusieurs heures, lancer le serveur dans une session `tmux` ou un service système dédié plutôt que dans une fenêtre SSH susceptible de se fermer. Si l'interface redémarre, les tâches sont récupérées depuis `~/partage-freebox-migration/migration.sqlite3`.

## Utilisation

1. Choisir un disque et parcourir ses dossiers ; cocher les fichiers/dossiers à migrer.
2. Choisir le dossier cible dans la hiérarchie Partage ; la structure des dossiers cochés est conservée.
3. Cliquer sur **Calculer la sélection** pour construire le manifeste complet (taille totale, nombre de fichiers et exemples), puis **Lancer les transferts**.
4. Suivre chaque fichier. Les erreurs réseau déclenchent automatiquement une nouvelle tentative après 30 s, 1 min, 2 min, puis 5 min au maximum ; les fichiers défectueux ou sources modifiées demandent une action manuelle. Les transferts sont séquentiels afin de ne pas saturer la Freebox.
5. Pour le premier essai, **choisir un tout petit fichier**, confirmer son téléchargement et sa visibilité dans Partage avant d'importer un gros dossier.

La sélection courante (cases cochées et destination) est **enregistrée sur le VPS** et restaurée après fermeture/rafraîchissement du navigateur. Elle est réinitialisée après confirmation de la mise en file.

Les fichiers déjà transférés par **cette migration** sont ignorés seulement si la provenance, la taille, la date, le dossier cible et l'enregistrement du catalogue concordent. Les tâches en cours, en pause ou en erreur ne sont pas dupliquées ; elles restent récupérables dans le suivi. Les fichiers de même nom provenant d'ailleurs ne sont **jamais écrasés** : une collision bloque la préparation. L'outil refuse également les liens symboliques et les sélections qui sortent des partitions montées.

La zone **Transferts vers S3** fournit un lien **Rapport CSV** contenant l'ensemble des tâches historiques et leurs statuts, sans la limite de 500 lignes de la vue à l'écran.

### Reprise

La base temporaire stocke, pour chaque fichier, un objet S3 unique, son upload multipart, les parties validées et les octets déjà envoyés. Sur redémarrage, le worker récupère les parties réellement présentes sur S3 avant de continuer ; la Freebox peut ignorer `Range`, dans ce cas les octets déjà transférés sont relus puis ignorés en flux, sans fichier temporaire. Si un fichier source a changé de taille/date, le transfert s'arrête plutôt que mélanger deux versions.

Le système vérifie la **taille de l'objet publié dans S3** avant insertion en base Partage. Il ne calcule pas actuellement un hash cryptographique complet Freebox↔S3 : le contrôle garantit la longueur et les checkpoints S3, pas une preuve d'identité bit à bit.

### Rapport de vérification avant retrait de la Freebox

Après la fin des copies, exécuter une réconciliation **en lecture seule** entre la base de migration, le catalogue Partage et chaque objet S3 :

```sh
cd ~/partage-fbx-migration
PARTAGE_ENV_FILE=/opt/partage/.env .venv/bin/python audit.py
```

Changer le chemin de `PARTAGE_ENV_FILE` uniquement si le `.env` existant est ailleurs. Un rapport CSV privé `~/partage-freebox-migration/audit-migration-*.csv` est créé, avec les états `valide`, `incomplet`, `absent_du_catalogue`, `catalogue_incoherent` et `taille_s3_incorrecte`. Corriger les anomalies avant de débrancher la Freebox.

Le contrôle compare la présence et la **taille**, mais pas un hash cryptographique de bout en bout. Il ne permet pas à lui seul d'affirmer une identité binaire parfaite.

### Redémarrage automatique du VPS (recommandé pour une migration longue)

1. Arrêter d'abord le serveur Uvicorn démarré manuellement sur le port 8791.
2. Adapter **uniquement** le chemin `PARTAGE_ENV_FILE` du modèle si nécessaire.
3. Installer le service utilisateur :

```sh
mkdir -p ~/.config/systemd/user
cp ~/partage-fbx-migration/deploy/partage-fbx-migration.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now partage-fbx-migration.service
systemctl --user status partage-fbx-migration.service --no-pager
```

Pour permettre au service utilisateur de redémarrer après un reboot même sans connexion SSH, activer une seule fois le linger (nécessite sudo) :

```sh
sudo loginctl enable-linger ubuntu
```

L'interface reste disponible **uniquement par le tunnel SSH**. Le service n'écoute que sur `127.0.0.1:8791`. Les jobs et checkpoints sont conservés dans la base privée. Pour arrêter définitivement après migration :

```sh
systemctl --user disable --now partage-fbx-migration.service
```

### Limites et sécurité

- Outil ponctuel à **un seul serveur et un seul worker**. Lancer exactement un worker Uvicorn.
- L'analyse récursive est limitée à 20 000 fichiers et 48 niveaux ; les arborescences plus vastes doivent être divisées.
- Les fichiers sont streamés en mémoire ; les uploads partiels inachevés restent dans S3 en cas d'abandon définitif. Après la migration, vérifier qu'aucun multipart ne reste ouvert dans le bucket.
- Les métadonnées images/vidéos sont ajoutées à la file normale de Partage après publication. L'encodeur existant n'est jamais redémarré par l'outil.
- La base de transferts et l'autorisation sont conservées dans le dossier privé `~/partage-freebox-migration`, jamais dans Git.
- Tests automatiques : `python -m unittest discover -s tests -v` ; aucune connexion Freebox/S3 requise.
- Ne jamais copier des données de `.env` dans le chat ou dans GitHub.

## Après la migration

Vérifier dans Partage les fichiers transférés et leurs volumes, puis arrêter Uvicorn ou le service. Dans Freebox OS, **révoquer l'application Partage Migration**. Après s'être assuré que plus aucun transfert ne reste en cours, supprimer le dépôt et le dossier de données/certificats/jetons temporaires. **Ne jamais supprimer les originaux sur la Freebox automatiquement.**
