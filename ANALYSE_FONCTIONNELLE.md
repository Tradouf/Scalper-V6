# Analyse fonctionnelle — Ricochet, XSMom et leurs tableaux de bord

*Rédigé le 2026-08-09. Document destiné à être lu sans connaissance technique
préalable. Il décrit ce que font les deux robots de trading actuellement en
service, ce que montrent leurs deux tableaux de bord, et pourquoi chaque
élément existe.*

---

## 1. Vue d'ensemble

Deux robots tournent aujourd'hui, avec deux paris complètement différents.

| | **Ricochet** | **XSMom** |
|---|---|---|
| Ce qu'il croit | Après une chute brutale, le prix rebondit un peu | Ce qui monte le mieux continue de mieux monter que le reste |
| Ce qu'il achète | Une crypto qui vient de toucher un creux | Les 8 meilleures **et** vend les 8 pires en même temps |
| Il gagne si | Le prix remonte dans les 4 heures | L'écart entre les meilleures et les pires se creuse |
| Le marché monte ou descend ? | Ça compte | **Ça ne compte pas** |
| Durée d'une position | 4 heures | 7 jours |
| Rythme des décisions | Toutes les heures | Une fois par jour |
| Argent réel ? | **Oui**, 208 $ | Non, papier uniquement |
| Verdict prévu | mi-septembre 2026 | mi-septembre 2026 |
| Tableau de bord | http://localhost:8085 | http://localhost:8086 |

Les deux suivent la même discipline, héritée d'échecs coûteux : **le critère
de réussite est fixé par écrit avant de commencer**, et on ne le change pas en
cours de route.

---

## 2. Ricochet — acheter les creux

### 2.1 L'idée

Quand le prix d'une crypto chute brutalement, il remonte souvent un peu juste
après. Ricochet achète pendant ce creux et revend quatre heures plus tard.

### 2.2 Pourquoi ça peut marcher

Une grosse baisse n'est pas toujours une opinion du marché. Souvent, c'est
quelqu'un qui est **obligé** de vendre : il avait emprunté pour acheter, le
prix a baissé, et la plateforme solde sa position de force. Ce vendeur ne
choisit ni son moment ni son prix — il subit. Quand il a fini de vendre, la
pression disparaît et le prix se redresse.

Ricochet essaie d'être l'acheteur en face de ce vendeur contraint.

### 2.3 Ce qu'il fait, chaque heure

Il examine 48 cryptos et se pose trois questions.

**Est-ce que ça vient de rebondir après une chute ?**
Il utilise le RSI, un indicateur classique noté de 0 à 100 qui mesure si un
actif a été beaucoup vendu. En dessous de 30, on parle de « survente ». Le bot
attend que l'indicateur **repasse au-dessus de 30** : la chute est finie, ça
remonte. C'est son signal d'achat.

**Est-ce que le marché bouge assez ?**
C'est le filtre le plus important, et le moins évident. La mesure est nette :
quand le marché est calme, ce rebond ne rapporte **rien du tout**, même avant
de compter les frais. Le gain n'existe que quand ça bouge.

| État du marché | Ce qu'il fait | Pourquoi |
|---|---|---|
| Calme | **Il n'achète pas** | Aucun gain mesurable, même sans frais |
| Normal | Achat plein (~25 $) | C'est là que se trouve le gain |
| Agité | Achat réduit de moitié | Gain plus élevé, mais risque quatre fois plus grand |

À noter : « calme » veut dire calme **pour cette crypto-là**, comparé à son
propre passé récent — pas calme dans l'absolu.

**Y a-t-il de la place ?**
Huit positions ouvertes au maximum, et jamais plus d'argent engagé qu'il n'y
en a sur le compte. Autrement dit, pas d'emprunt, donc pas de risque de tout
perdre d'un coup.

Puis il achète, et **il revend exactement quatre heures plus tard**, quoi qu'il
arrive : ni objectif de gain, ni seuil de perte. Toutes les variantes avec des
seuils ont été testées et font moins bien. La raison est simple : pendant ces
quatre heures, le bot n'apprend rien de nouveau, donc réagir aux soubresauts
ne fait que rogner le gain.

### 2.4 Les garde-fous

- **Arrêt d'urgence** : si le compte perd 5 % par rapport à son meilleur
  niveau des dernières 24 heures, tout est fermé et le bot s'arrête 24 heures.
  Il vérifie deux fois avant de déclencher, pour ne pas réagir à une lecture
  erronée.
- **Gel** : s'il n'arrive plus à lire le solde du compte trois fois de suite,
  il **arrête de trader** plutôt que d'agir à l'aveugle.
- **Refus de démarrer** : si le solde lu vaut zéro, il refuse de se lancer.
  Un solde à zéro ne veut presque jamais dire « compte vide » — ça veut dire
  qu'on lit la mauvaise adresse. Ce garde-fou a déjà attrapé un vrai bug.
- **Ordres patients** : il essaie toujours de proposer son prix et d'attendre
  plutôt que de payer le prix du marché, ce qui économise des frais.

### 2.5 Ce qu'on en attend

Environ 13 occasions par jour sur les 48 cryptos, dont 8 sur 10 passent le
filtre. Le gain espéré est d'environ **0,15 % par opération**, soit à peu près
4 centimes sur 25 $. C'est minuscule à l'unité : ça ne devient significatif
qu'en le répétant. Sur ce compte de 208 $, l'ordre de grandeur est de
**15 à 30 $ par mois**, en gain comme en perte.

### 2.6 Ce qu'on ne sait pas encore

Le test historique est solide : validé sur 200 jours, dont 115 jamais utilisés
pour la mise au point, et il passe le test anti-hasard (on rejoue la stratégie
sur des données mélangées où aucun gain n'est possible ; elle ne s'y trompe
pas). Mais l'historique du projet est sans pitié : une stratégie précédente
avait un test magnifique et a perdu 21 % en trois semaines de réel.

C'est pourquoi **une copie du bot tourne en parallèle sur papier**, sans
argent, avec la règle *non filtrée* — elle prend tous les signaux, y compris
en marché calme. Elle sert de juge impartial, et la comparaison entre les deux
mesurera ce que le filtre apporte réellement.

### 2.7 Le collecteur de liquidations

Un troisième programme tourne à côté, sans jamais passer d'ordre. Il enregistre
en continu les **liquidations forcées** — la cause même du phénomène exploité.

Il ne peut pas les lire directement : la plateforme ne publie aucune liste de
liquidations. Il les repère donc à leur trace, puis les confirme une par une en
interrogeant les comptes concernés.

Deux enseignements y ont été obtenus, tous deux contre-intuitifs :

- **La trace fiable est la baisse de l'« intérêt ouvert »** (le total des
  positions en cours). C'est mécanique : une liquidation ferme une position,
  donc ce total baisse. Cette trace, combinée à la taille des transactions,
  identifie correctement une liquidation une fois sur deux, contre une fois
  sur cent pour la première méthode essayée.
- **Le sens se lit dans le prix** : vente forcée quand le prix baisse, achat
  forcé quand il monte — correct dans 79 cas sur 79. Ce résultat avait d'abord
  semblé faux à cause d'une erreur d'étiquetage de ma part : les données
  décrivent le point de vue de *l'acheteur en face*, pas celui du liquidé.
  Quand un acheteur à crédit est liquidé, c'est sa contrepartie qui **achète**.

Ricochet ne s'en sert pas encore pour décider. Ces données s'accumulent pour
qu'on puisse un jour lui poser la vraie question au moment d'acheter :
*le vendeur contraint a-t-il fini de vendre ?*

---

## 3. XSMom — parier sur un classement

### 3.1 L'idée

Il achète les cryptos qui montent le mieux et vend celles qui montent le moins,
**en même temps**. Il gagne sur l'écart entre les deux, pas sur la direction du
marché.

### 3.2 Neutre au marché : ce que ça veut dire

Ricochet parie que le prix va monter. XSMom ne parie sur rien de tel. Chaque
jour, il classe 40 cryptos de la meilleure à la moins bonne, achète les 8
premières et vend à découvert les 8 dernières, pour des montants égaux.

Si tout le marché s'effondre, ses achats perdent — mais ses ventes à découvert
gagnent à peu près autant. Ce qui lui reste, c'est uniquement la différence
entre le haut et le bas du classement. Il se moque de savoir si le bitcoin
monte ou descend ; il lui faut seulement que les gagnants continuent de faire
mieux que les perdants.

### 3.3 Comment il classe

Une division très simple : **le gain des 14 derniers jours, divisé par
l'agitation des 20 derniers**.

Le numérateur, c'est l'élan : ce qui monte depuis deux semaines a tendance à
continuer un moment. Le dénominateur est la partie subtile — il pénalise les
cryptos qui bougent énormément. Sans lui, le classement serait toujours occupé
par les plus agitées, qui flambent puis s'effondrent. Diviser par l'agitation,
c'est demander « quel gain, pour combien de secousses ? » et préférer les
hausses régulières aux feux de paille.

### 3.4 L'astuce des 7 tranches

C'est l'élément le plus important et le moins intuitif. Le portefeuille est
découpé en **7 tranches**, et chaque jour **une seule** est renouvelée. Chaque
position vit donc 7 jours, et le portefeuille se renouvelle par septièmes.

La raison vient d'une leçon apprise à ses dépens. La version simple — « je
rééquilibre tous les lundis » — donnait d'excellents résultats… le lundi. En
essayant les autres jours, la performance passait d'excellente à nulle selon le
jour choisi. C'était donc de la chance de calendrier, pas un vrai gain. En
renouvelant un septième chaque jour, le bot est présent tous les jours à la
fois : le choix du jour ne peut plus le flatter ni le pénaliser. C'est la seule
version qui a survécu à ce test.

Conséquence pratique : il détient **112 positions en permanence**
(7 tranches × 16), chacune d'environ 9 $.

### 3.5 Ce qui tourne aujourd'hui

En papier depuis le 23 juillet, avec 1 000 $ fictifs. Aucun argent réel n'est
engagé. L'infrastructure pour passer en réel existe et vient d'être corrigée,
mais elle est **volontairement désarmée** jusqu'au verdict.

### 3.6 Ce qu'on en attend

C'est le meilleur candidat jamais trouvé sur ce projet : testé sur 833 jours,
sur une liste de cryptos construite pour éviter le piège du survivant (ne
retenir que celles qui existent encore aujourd'hui gonfle artificiellement les
résultats), frais compris. Gain espéré de l'ordre de **5 à 10 points de base
par jour** (0,05 à 0,10 %), avec une baisse maximale historique d'environ 23 %.

Mais — c'est écrit dans son propre code — **ce n'est pas une preuve**. La
solidité statistique est correcte sans être écrasante, et une quinzaine de
variantes ont été essayées avant de retenir celle-ci, ce qui affaiblit la
garantie.

Point pratique : il faudra au minimum **1 250 $** pour le faire tourner en
réel, parce que 112 positions simultanées avec un minimum de 10 $ par ordre ne
tiennent pas dans un compte plus petit.

---

## 4. Le tableau de bord de Ricochet (port 8085)

Chaque bloc répond à une question précise. L'ordre n'est pas décoratif : il va
du plus urgent au plus documentaire.

### 4.1 Le bandeau du haut

Affiche en rouge **ORDRES RÉELS** ou en vert **DRY-RUN**. Cette information
est **lue dans l'état du bot**, jamais devinée : un tableau de bord qui se
trompe sur ce point est pire que pas de tableau de bord du tout.

### 4.2 Les quatre chiffres clés

L'argent sur le compte, le résultat encaissé, le nombre d'opérations
terminées, et le nombre de positions ouvertes sur les 8 possibles.

Le solde vient d'une lecture qui additionne correctement les deux poches du
compte. C'est important : la lecture naïve n'affiche que la partie engagée en
garantie et donnerait zéro la plupart du temps.

### 4.3 Les positions en cours

Pour chaque position : la taille, le prix d'achat, le montant, l'état du
marché au moment de l'achat, **le temps restant avant la revente automatique**,
et le gain ou la perte en cours.

La colonne la plus importante est la dernière, « État », qui a trois valeurs :

- **suivie** — le bot la connaît et elle existe bien sur la plateforme ;
- **absente de la plateforme** — le bot croit détenir quelque chose qui n'y
  est pas ;
- **inconnue du bot** — une position existe et le bot l'ignore, donc **il ne
  la fermera jamais**.

Ce tableau est construit à partir des **deux sources réunies** : ce que croit
le bot et ce que dit réellement la plateforme. N'afficher que la vision du bot
masquerait précisément le cas dangereux. Toute divergence déclenche en plus une
bannière d'alerte.

### 4.4 Les signaux non pris, et pourquoi

Un compteur par raison : marché trop calme, montant sous le minimum de la
plateforme, huit positions déjà ouvertes, plafond d'exposition atteint.

Ce bloc existe pour une raison précise : **sans lui, un bot qui ne trade rien
est indiscernable d'un bot qui n'a pas de signal**. C'est exactement ce qu'un
bug récent aurait produit — un bot parfaitement sain en apparence, ne faisant
rien pendant des semaines.

### 4.5 Le papier, et le collecteur

Les résultats de la copie sans argent, qui reste le juge jusqu'à mi-septembre.
Puis l'état du collecteur de liquidations : combien enregistrées, combien
d'alertes déclenchées, et combien se sont révélées être de vraies liquidations
— c'est ce dernier chiffre qui dit si la méthode de détection vaut quelque
chose.

---

## 5. Le tableau de bord de XSMom (port 8086)

Les blocs sont différents, parce que les deux bots peuvent casser de façons
différentes.

### 5.1 La neutralité au marché — en premier

Le montant acheté d'un côté, le montant vendu à découvert de l'autre, l'écart
en euros et en pourcentage, avec une barre visuelle et une alerte au-delà de
15 %.

C'est l'hypothèse dont tout dépend. Si les deux côtés se déséquilibrent, le
bot n'est plus neutre : son résultat se met à dépendre de la direction du
marché, ce qu'il n'est pas censé faire. Actuellement les deux côtés sont
rigoureusement égaux.

### 5.2 L'exposition par symbole, toutes tranches confondues

Le point technique important. Un même symbole peut être **acheté dans une
tranche et vendu dans une autre**. Le lire tranche par tranche donnerait une
image fausse du risque réel : deux lignes contradictoires au lieu d'un chiffre
juste.

Le tableau additionne donc tout et montre, pour chaque crypto : le montant net,
le montant brut, dans combien de tranches elle vit, et le gain latent.

Cela rend visible une chose qu'on ne verrait pas autrement : quand une crypto
reste bien classée plusieurs jours, elle finit présente dans **les 7 tranches
à la fois**. C'est le comportement normal du système — mais c'est bon à savoir,
car les tranches lissent le moment d'entrée, pas la concentration.

### 5.3 La performance face au critère fixé d'avance

Le gain par jour, affiché **à côté de la fourchette attendue** (+5 à 10 points
de base), et le nombre de jours restants avant le verdict.

C'est délibéré : voir la cible en même temps que le résultat empêche de la
redéfinir après coup, ce qui est la façon la plus courante de se mentir à
soi-même sur une stratégie.

### 5.4 Le contexte de marché — pour comprendre, pas pour décider

Le mouvement de l'ensemble du marché sur 24 heures (pondéré par les volumes,
ce qui approche un indice de capitalisation), la proportion de cryptos en
hausse, et les plus gros mouvements du jour.

**Ce bloc ne sert pas à filtrer les trades.** La question a été mesurée sur
206 jours : l'état du marché ne prédit pas le résultat de XSMom. La
corrélation avec le marché du moment est de −0,07, ce qui confirme au passage
que la neutralité fonctionne, et aucun indicateur de phase ou de dispersion ne
ressort du bruit.

Son utilité est ailleurs, et elle est réelle : **si la stratégie déçoit, savoir
ce que faisait le marché permet de distinguer deux causes très différentes**.
Le momentum a un mode de défaillance connu — après une forte baisse, un rebond
violent fait remonter le plus fort ce qui avait le plus chuté, c'est-à-dire
exactement ce que la stratégie a vendu à découvert. Ce phénomène est rare : il
n'y en a probablement aucun exemple dans les 206 jours mesurés, ce qui explique
qu'on ne puisse pas le chiffrer. On ne peut donc pas s'en protéger à l'avance,
mais on peut le **reconnaître** s'il survient — à condition de regarder le
marché au bon moment.

C'est aussi la vérification permanente de l'hypothèse centrale : si la
corrélation avec le marché s'éloigne durablement de zéro, la stratégie n'est
plus neutre et le problème est structurel.

### 5.5 Les 7 tranches et les rééquilibrages

Combien de positions dans chaque tranche, laquelle est renouvelée aujourd'hui,
et une alerte si l'une est incomplète — signe de données manquantes ou de
montants sous le minimum. Puis l'historique des derniers rééquilibrages avec
leur résultat et les cryptos choisies.

### 5.6 Un détail d'efficacité

Tous les prix sont obtenus en **un seul appel** à la plateforme, mis en cache
20 secondes. Rafraîchir la page ne consomme donc quasiment rien. Et si les prix
sont indisponibles, le tableau affiche « — » plutôt qu'un zéro trompeur.

---

## 6. Ce que les deux ont en commun

**Le papier est le juge.** Un bon résultat sur données historiques ne prouve
rien : il est toujours possible de trouver après coup une règle qui aurait bien
marché. Seul le comportement sur des données jamais vues compte.

**Le critère est écrit avant.** Chaque stratégie a, dans son propre code, la
condition de réussite et la date du verdict. Cela évite le glissement
classique : constater un résultat médiocre, puis se convaincre que c'était en
fait l'objectif.

**Le test anti-hasard est obligatoire.** Avant tout engagement d'argent, la
stratégie est rejouée sur des données mélangées où aucun gain n'est possible.
Si elle « trouve » quand même quelque chose, c'est que la méthode de sélection
fabrique du signal à partir de bruit — et la stratégie est abandonnée. C'est ce
test qui a définitivement tué le bot précédent.

**Les paramètres sont gelés pendant le test.** Aucun ajustement en cours de
route. Un réglage modifié en cours de test invalide le test.

**Les mêmes sécurités.** Arrêt d'urgence avec double confirmation, gel si les
lectures deviennent impossibles, refus de démarrer sur un solde nul, ordres
patients pour économiser les frais, verrou empêchant deux instances du même bot
de tourner en même temps.

---

## 7. Piloter tout ça

Chaque programme est un service qui redémarre automatiquement en cas de
plantage et se relance au démarrage de la machine.

| Service | Rôle |
|---|---|
| `rsimr-live` | Ricochet, **argent réel** |
| `rsimr` | Ricochet, copie papier (le juge) |
| `liqfeed` | Collecteur de liquidations (n'ordonne rien) |
| `rsimr-dashboard` | Tableau de bord Ricochet, port 8085 |
| `xsmom` | XSMom, papier |
| `xsmom-dashboard` | Tableau de bord XSMom, port 8086 |

Commandes utiles (préfixées par `systemctl --user`) :

- `status <service>` — voir s'il tourne ;
- `stop <service>` — l'arrêter ;
- `restart <service>` — le relancer ;
- `disable --now <service>` — l'arrêter **et** empêcher son redémarrage
  automatique. C'est la commande à utiliser pour arrêter Ricochet pour de bon.

Pour **désarmer Ricochet** sans le supprimer : remplacer `RSIMR_DRY_RUN=0` par
`RSIMR_DRY_RUN=1` dans son fichier de service, puis `daemon-reload` et
`restart`. Il continue de tourner et de calculer, mais ne passe plus aucun
ordre.

Les journaux sont dans `logs/` (`rsimr_live.log`, `liqfeed.log`, etc.).

---

## 8. Points de vigilance

**Ricochet engage de l'argent réel avant son verdict.** C'est un choix
assumé : le montant est petit et l'exposition plafonnée, mais le bot anticipe
volontairement le résultat du test papier.

**Le code d'exécution est jeune.** Il a été écrit récemment. Les sécurités sont
reprises d'un bot qui a réellement tourné en production, et il est couvert par
des tests automatiques, mais il a passé très peu d'ordres réels à ce jour.

**Les deux verdicts tombent en même temps**, à la mi-septembre. Il faudra
résister à deux tentations symétriques : prolonger un test qui déçoit en
espérant un retournement, et engager davantage d'argent sur un test qui réussit
avant qu'il soit terminé.

**XSMom est actuellement sous sa cible** (résultat négatif après 17 jours, là
où le backtest prévoyait un gain). Sur cette durée, cela ne prouve rien — mais
c'est à surveiller, et c'est précisément pour cela que le chiffre est affiché
en permanence à côté de sa cible.

---

## 9. Petit glossaire

- **Position** : un pari en cours sur une crypto, à la hausse ou à la baisse.
- **Vendre à découvert** : parier à la baisse ; on gagne si le prix chute.
- **Point de base (bps)** : un centième de pourcent. 15 bps = 0,15 %.
- **Liquidation** : fermeture forcée par la plateforme quand quelqu'un a
  emprunté pour investir et que sa garantie devient insuffisante.
- **Intérêt ouvert** : le total de toutes les positions en cours sur une
  crypto. Il baisse quand des positions se ferment.
- **RSI** : indicateur de 0 à 100 mesurant si un actif a été beaucoup acheté
  ou beaucoup vendu récemment.
- **Papier (paper)** : le bot calcule et enregistre tout, mais ne passe aucun
  ordre. Aucun argent en jeu.
- **Maker / taker** : proposer son prix et attendre (moins cher) contre
  accepter le prix affiché (plus cher, immédiat).
- **Backtest** : rejouer une stratégie sur des données passées.
- **Compte unifié** : mode où le solde disponible sert directement de garantie
  aux positions, sans transfert interne.
