# Generateur d'antenne PCB reconfigurable (3 bandes)

Script: `pcb_antenna_generator.py`

## Objectif
Cet outil calcule et genere une geometrie d'antenne PCB reconfigurable pour 3 frequences cibles, sur la base d'un resonateur quart d'onde imprime (microstrip) avec 3 branches commutables.

## Equations implementees
- Permittivite effective (Hammerstad/Jensen)
- Correction de frange ouverte (Hammerstad)
- Longueur quart d'onde guidee
- Longueur physique branche = `L_qw - DeltaL - L_switch`

## Lancer l'application
```bash
python3 pcb_antenna_generator.py
```

## Parametres principaux
- Frequence 1/2/3 (MHz)
- `er` du substrate
- Epaisseur substrate `h` (mm)
- Largeur piste `w` (mm)
- Longueur reservee switch (diode PIN/switch RF)
- Parametres de meandre et marge mecanique

## Sorties
- Rapport de calcul detaille dans la fenetre
- Export DXF (`.dxf`) de la geometrie meandree
- Sauvegarde du rapport en texte

## Notes d'ingenierie
- Le resultat est une base de pre-dimensionnement.
- Une simulation EM (S11, rendement, diagramme) est necessaire avant fabrication.
- Le plan de masse, le boitier, les batteries et les composants proches peuvent fortement deplacer la resonance.
- Il est recommande de garder des longueurs ajustables (zones de trim) sur chaque branche.
