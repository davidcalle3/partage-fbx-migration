# Partage · Migration Freebox

Outil temporaire, **indépendant** du site Partage et de son encodeur : navigation Freebox OS en lecture seule, sélection de fichiers/dossiers, prévisualisation de la taille, copie directe Freebox → VPS (en mémoire, par blocs de 8 Mio) → S3 OVH, suivi et reprise des transferts.

**La source Freebox n'est jamais modifiée.** La seule écriture dans Partage est la création des dossiers sélectionnés et la publication des fichiers **après** vérification de leur présence et de leur taille dans S3. Aucun fichier média complet n'est conservé sur le VPS. Les vidéos importées suivent ensuite le traitement vidéo normal de Partage.

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

5. Sur votre ordinateur (dans **un second terminal**, pas sur le VPS), ouvrir le tunnel SSH :

   ```sh
   ssh -N -L 8791:127.0.0.1:8791 ubuntu@IP_DU_VPS
   ```

   Puis ouvrir **http://127.0.0.1:8791** dans votre navigateur. Le port **8791 n'est pas exposé publiquement**. Ne pas ouvrir le pare-feu ou configurer nginx pour cet outil : son interface n'a pas de connexion utilisateur, car elle est volontairement limitée au tunnel SSH.

Pour les copies de plusieurs heures, lancer le serveur dans une session `tmux` ou un service système dédié plutôt que dans une fenêtre SSH susceptible de se fermer. Si l'interface redémarre, les tâches sont récupérées depuis `~/partage-freebox-migration/migration.sqlite3`.

## Utilisation

1. Choisir un disque et parcourir ses dossiers ; cocher les fichiers/dossiers à migrer.
2. Choisir le dossier cible dans la hiérarchie Partage ; la structure des dossiers cochés est conservée.
3. Cliquer sur **Calculer la sélection** pour construire le manifeste complet (taille totale, nombre de fichiers et exemples), puis **Lancer les transferts**.
4. Suivre chaque fichier, le mettre en pause, reprendre ou réessayer les échecs. Les transferts sont séquentiels afin de ne pas saturer la Freebox.
5. Pour le premier essai, **choisir un tout petit fichier**, confirmer son téléchargement et sa visibilité dans Partage avant d'importer un gros dossier.

Les fichiers de même nom dans le dossier de destination ne sont **jamais écrasés** : une collision bloque la publication. L'outil refuse également les liens symboliques et les sélections qui sortent des partitions montées.

### Reprise

La base temporaire stocke, pour chaque fichier, un objet S3 unique, son upload multipart, les parties validées et les octets déjà envoyés. Sur redémarrage, le worker récupère les parties réellement présentes sur S3 avant de continuer ; la Freebox peut ignorer `Range`, dans ce cas les octets déjà transférés sont relus puis ignorés en flux, sans fichier temporaire. Si un fichier source a changé de taille/date, le transfert s'arrête plutôt que mélanger deux versions.

Le système vérifie la **taille de l'objet publié dans S3** avant insertion en base Partage. Il ne calcule pas actuellement un hash cryptographique complet Freebox↔S3 : le contrôle garantit la longueur et les checkpoints S3, pas une preuve d'identité bit à bit.

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
