# PUBG recap — GitHub Actions

Un run GitHub tourne en continu (boucle de ~5h40, puis se relance lui-même via le secret `GH_PAT`). Toutes les 5 min, il :
1. récupère les nouveaux matchs ranked du cercle ;
2. poste le recap dès la première game et l'édite après chaque game (même lineup = même message, toute la journée), suivi d'un second message "Détail" (tableaux façon Twire, stats télémétrie) édité en même temps ;
3. poste les nouveaux clips pubg.report ;
4. sauvegarde `recap.db` dans le repo.

## Mise en place (une fois)

1. Créer un repo **privé** sur github.com.
2. Y mettre : `pubg_recap.py`, `config.json`, `recap.db` (celui de ton PC, pour ne rien reposter), et `.github/workflows/pubg.yml`.
   Via le site : "Add file → Upload files" pour les trois premiers, puis "Add file → Create new file" avec le nom `.github/workflows/pubg.yml` et coller le contenu.
3. Settings → Secrets and variables → Actions → New repository secret, trois fois :
   - `PUBG_API_KEY`
   - `DISCORD_WEBHOOK`
   - `DISCORD_WEBHOOK_REPORTS`
   - `GH_PAT` : token fine-grained (Settings → Developer settings → Fine-grained tokens), repo `pubg-recap` seul, permission Actions : Read and write. Sert au workflow pour se relancer.
4. Settings → Actions → General → Workflow permissions → **Read and write permissions** → Save (nécessaire pour que le bot commit la base).
5. Onglet Actions → "PUBG recap" → "Run workflow" pour un premier passage.

## Si ça s'arrête

Onglet Actions → "PUBG recap" → "Run workflow". Un seul run doit être en cours à la fois (le `concurrency` s'en charge).

## Forcer un recap

Onglet Actions → "PUBG recap" → "Run workflow" → cocher "Forcer le recap".

## Config

`config.json` : `circle`, `aliases`, `day_cutoff_hour`, `detail` (false pour couper le second message). Les clés ne sont pas dans ce fichier, elles viennent des secrets.
