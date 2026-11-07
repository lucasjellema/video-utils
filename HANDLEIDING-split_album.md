# Handleiding: een album opsplitsen in losse nummers

Heb je een album als **één groot audiobestand** (bijvoorbeeld `.m4a` of `.mp3`)? Dan maakt `split_album.py` daar losse bestanden van, **één per nummer**. Elk bestand krijgt meteen de juiste gegevens: titel, artiest, album, tracknummer, jaar, genre en de albumhoes. Je muziekspeler toont het album daarna netjes.

De tracklijst haalt het programma zelf op uit [MusicBrainz](https://musicbrainz.org), een gratis online muziekdatabase. Daar staat de volgorde van de nummers, hun lengte en hun titel. Je hoeft dus niets over te typen.

---

## 1. Eenmalig: installeren

Je hebt drie dingen nodig. Dit doe je maar één keer.

**a. Python** (versie 3.10 of nieuwer)
Download het van <https://www.python.org/downloads/>. Vink tijdens de installatie **"Add Python to PATH"** aan.

**b. ffmpeg** (het programma dat het knippen doet)
Open PowerShell en typ:

```bash
winget install Gyan.FFmpeg
```

Sluit PowerShell daarna en open hem opnieuw.

**c. De hulpbibliotheek voor de tags**
Ga in PowerShell naar de map waar `split_album.py` staat en typ:

```bash
python -m pip install -r requirements.txt
```

**Controleren of alles werkt:**

```bash
python split_album.py --help
```

Zie je een lijst met opties? Dan ben je klaar. Krijg je een foutmelding, kijk dan bij [Problemen oplossen](#6-problemen-oplossen).

> 💡 **PowerShell openen in de juiste map:** open de map met `split_album.py` in de Verkenner, klik in de adresbalk, typ `powershell` en druk op Enter.

---

## 2. De snelste manier

**Stap 1 – Geef je bestand een goede naam:** `Artiest - Album`. Bijvoorbeeld:

```
Pink Floyd - Wish You Were Here.m4a
```

Toevoegingen als `(1975)` of `[Full Album]` mogen blijven staan; het programma negeert ze.

**Stap 2 – Start het programma** met het pad naar je bestand. Zet het pad tussen **aanhalingstekens**, zeker als er spaties in staan:

```bash
python split_album.py "C:\Users\jij\Music\Pink Floyd - Wish You Were Here.m4a"
```

> 💡 **Tip:** typ `python split_album.py ` (met een spatie aan het eind) en sleep het audiobestand daarna vanuit de Verkenner naar het PowerShell-venster. Het pad wordt dan voor je ingevuld.

**Stap 3 – Wacht even.** Een album van 45 minuten duurt meestal 10 tot 20 seconden.

**Klaar!** Naast je originele bestand staat nu een nieuwe map:

```
Pink Floyd\
  1975 - Wish You Were Here\
    01 - Shine On You Crazy Diamond (Part One).m4a
    02 - Welcome to the Machine.m4a
    03 - Have a Cigar.m4a
    04 - Wish You Were Here.m4a
    05 - Shine On You Crazy Diamond (Part Two).m4a
    cover.jpg
    tracks.md
```

Je originele bestand blijft gewoon staan. Het programma verandert er niets aan.

---

## 3. Eerst kijken, dan knippen (aanrader)

Twijfel je of het goed gaat? Voeg dan `--dry-run` toe. Het programma laat zien wat het **zou** doen, maar schrijft niets weg:

```bash
python split_album.py "Pink Floyd - Wish You Were Here.m4a" --dry-run
```

Ziet het overzicht er goed uit? Draai dan hetzelfde commando opnieuw, maar zonder `--dry-run`.

---

## 4. Het overzicht lezen

Op het scherm en in `tracks.md` verschijnt een tabel zoals deze:

```
#  Title                   Length  Δ      Cut      File                             Start
1  Shine On You Crazy ...  13:31   -0.7s  silence  01 - Shine On You Crazy ....m4a  0:00
2  Welcome to the Machine  7:23    -3.1s  silence  02 - Welcome to the Machine.m4a  13:32
...
```

| Kolom | Betekenis |
|---|---|
| **#** | Tracknummer op het album (bij meerdere cd's bijvoorbeeld `2-3`: cd 2, nummer 3) |
| **Length** | Lengte van het geknipte nummer |
| **Δ** | Hoeveel het geknipte nummer afwijkt van de officiële lengte. Een paar seconden is normaal. |
| **Cut** | Hoe het einde van het nummer is gevonden. **silence**: midden in de stilte tussen twee nummers (het beste resultaat). **metadata** of **scaled**: geen stilte gevonden, dus geknipt op de officiële lengte. Controleer zo'n overgang even. **end**: het laatste nummer, loopt door tot het einde van het bestand. **cue**: exact volgens een cue-bestand. |
| **File** | Naam van het nieuwe bestand |
| **Start** | Waar het nummer begon in het originele bestand |
| **In file** | Verschijnt alleen als de nummers in een andere volgorde in je bestand stonden (zie [hoofdstuk 5](#nummers-staan-in-een-andere-volgorde-in-het-bestand)) |

**Vuistregel:** staat er overal `silence` en is Δ klein (een paar seconden)? Dan is alles waarschijnlijk perfect gegaan.

---

## 5. Situaties en oplossingen

### Mijn bestand heeft geen duidelijke naam

Geef de artiest en het album dan zelf op:

```bash
python split_album.py "opname.m4a" --artist "Pink Floyd" --album "Wish You Were Here"
```

### Het programma kiest de verkeerde uitgave van het album

Van veel albums bestaan meerdere uitgaven (een remaster, een deluxe-versie, een uitgave voor een ander land). Het programma kiest de uitgave waarvan de totale lengte het best bij je bestand past. Bovenaan de uitvoer zie je welke het werd, met daaronder alternatieven:

```
Release (closest in length of 132 editions): f973f4e5-9c7d-...  1984-09, GB, CD, 5 tracks
  runners-up (use --release-id to pick one):
    709229ba-e832-...  1986, XE, CD, 5 tracks, ...
```

Wil je een andere uitgave? Kopieer de lange code (het "release-id") en geef die mee:

```bash
python split_album.py "album.m4a" --release-id 709229ba-e832-4471-9441-6ef00c0fdce0
```

Op [musicbrainz.org](https://musicbrainz.org) kun je ook zelf zoeken. Het release-id staat in de adresbalk van de pagina van een uitgave.

### Foutmelding: "the file is … longer/shorter than the album"

Je bestand is meer dan 5% langer of korter dan het album volgens MusicBrainz. Meestal betekent dat:
- dat het om een **andere uitgave** gaat (bijvoorbeeld met bonustracks). Kies dan een andere met `--release-id`, zie hierboven.
- of dat het **een ander album** is dan je dacht. Controleer de artiest en de albumnaam.

Weet je zeker dat het klopt, bijvoorbeeld omdat er een lange stilte aan het eind van je bestand staat? Dan dwing je het knippen af met `--force`.

### Nummers staan in een andere volgorde in het bestand

Geen probleem: het programma merkt dit meestal zelf. Je ziet dan bijvoorbeeld:

```
Order : the file holds the tracks in this order: 3, 1, 5, 2, 4
```

De bestanden krijgen toch de juiste albumnummers (`01 - …`, `02 - …`), en de kolom **In file** laat zien waar elk nummer in je bestand stond.

**Krijg je een waarschuwing dat twee nummers "about the same length" hebben?** Dan zijn ze bijna even lang, en kan het programma ze op lengte alleen niet betrouwbaar uit elkaar houden. Luister die twee nummers even na. Zijn ze verwisseld? Geef de volgorde dan zelf op. Noem de albumnummers in de volgorde waarin ze in je bestand staan:

```bash
python split_album.py "album.m4a" --order 3,1,5,2,4
```

Bij een album met meerdere cd's schrijf je `cd-nummer`, bijvoorbeeld `--order 2-1,1-1,1-2`.

Wil je dat het programma altijd de albumvolgorde aanhoudt? Gebruik dan `--order album`.

> 💡 **Nog betrouwbaarder: AcoustID.** Met een gratis AcoustID-sleutel herkent het programma elk nummer aan het geluid zelf, zoals Shazam. Vraag een sleutel aan op <https://acoustid.org/new-application> en geef hem mee:
> ```bash
> python split_album.py "album.m4a" --acoustid-key JOUWSLEUTEL
> ```
> De sleutel wordt alleen gebruikt als het programma de volgorde moet uitzoeken.

### Het album staat niet in MusicBrainz, of je hebt geen internet

Maak dan zelf een tracklijst in een tekstbestand (bijvoorbeeld met Kladblok) en gebruik `--tracklist`. Er zijn twee manieren:

**a. Met starttijden.** Handig als je de tijden kent, bijvoorbeeld uit de beschrijving van een video:

```
Artist: Pink Floyd
Album: Wish You Were Here
Year: 1975
0:00 Shine On You Crazy Diamond (Part One)
13:31 Welcome to the Machine
20:58 Have a Cigar
26:06 Wish You Were Here
31:46 Shine On You Crazy Diamond (Part Two)
```

**b. Met lengtes:**

```
Artist: Pink Floyd
Album: Wish You Were Here
Shine On You Crazy Diamond (Part One) 13:31
Welcome to the Machine 7:26
Have a Cigar 5:08
Wish You Were Here 5:40
Shine On You Crazy Diamond (Part Two) 12:23
```

Bewaar het als bijvoorbeeld `tracks.txt` en start:

```bash
python split_album.py "album.m4a" --tracklist tracks.txt
```

Heb je een **cue-bestand** (`.cue`, zit vaak bij cd-rips)? Gebruik dat dan, want daarmee wordt exact geknipt:

```bash
python split_album.py "album.flac" --tracklist "album.cue"
```

Met een tracklijst krijg je geen albumhoes en geen MusicBrainz-gegevens.

### Een overgang klopt niet precies

Het programma zoekt binnen 5 seconden rond elk verwacht einde naar stilte. Opties om mee te spelen:
- `--snap-window 10`: zoek verder (10 seconden).
- `--silence-db -30`: ook zachte geluiden tellen als stilte. Handig bij opnames met ruis.
- `--no-snap`: knip precies op de officiële lengtes, zonder naar stilte te zoeken.

### Albums zonder pauzes (live-albums, nummers die in elkaar overlopen)

Hier is geen stilte om op te knippen, dus knipt het programma op de officiële lengtes (`metadata` in de kolom Cut). Dat is meestal goed tot op een paar seconden.

---

## 6. Problemen oplossen

| Melding of probleem | Oplossing |
|---|---|
| `'python' is not recognized…` | Python is niet (goed) geïnstalleerd. Installeer het opnieuw en vink **"Add Python to PATH"** aan. |
| `error: ffmpeg not found` | Installeer ffmpeg (zie [stap 1b](#1-eenmalig-installeren)) en open PowerShell opnieuw. |
| `the 'mutagen' package … is not installed for this Python` | Kopieer de installatieopdracht die in de melding staat en voer die uit. Die opdracht bevat het pad naar precies de Python die je gebruikt. Dat is belangrijk als je meerdere versies van Python op je computer hebt staan: een gewone `pip install` installeert dan soms in een andere versie. |
| `error: input file not found` | Het pad klopt niet. Zet het tussen aanhalingstekens, of sleep het bestand naar het venster. |
| `error: cannot tell the artist and album…` | Geef `--artist` en `--album` op, of hernoem het bestand naar `Artiest - Album.m4a`. |
| `Note : no exact match … using the closest: …` | De albumnaam in je tags wijkt iets af van die in MusicBrainz (bijvoorbeeld "Songs **from** …" tegenover "Songs **of** …"). Het programma gebruikt dan het album dat er het meest op lijkt. Controleer even of dat het juiste album is. |
| `error: no release found in MusicBrainz…` | Ook een album met een sterk gelijkende naam is niet gevonden. Controleer de spelling, geef `--artist` en `--album` zelf op, of zoek het album op [musicbrainz.org](https://musicbrainz.org) en gebruik `--release-id`. Werkt het nog steeds niet, gebruik dan een [tracklijst](#het-album-staat-niet-in-musicbrainz-of-je-hebt-geen-internet). |
| `note: no silences in the file` | Er zitten geen stiltes tussen de nummers (een live-album, of nummers die in elkaar overlopen), dus wordt er op de officiële lengtes geknipt. Zit er wat ruis in de pauzes? Probeer dan `--silence-db -30`. |
| `cannot reach MusicBrainz` | Controleer je internetverbinding, of gebruik een tracklijst. |
| `Cover : none in the Cover Art Archive` | Er is geen hoes van dit album beschikbaar. Je nummers zijn wel gewoon gemaakt. |
| Vreemde tekens in de titels op het scherm | Alleen een weergaveprobleem van PowerShell. De bestanden zelf zijn in orde. |

---

## 7. Alle opties op een rij

| Optie | Wat het doet |
|---|---|
| `--artist "…"` / `--album "…"` | Artiest en album zelf opgeven |
| `--release-id CODE` | Precies deze uitgave uit MusicBrainz gebruiken |
| `--tracklist BESTAND` | Eigen tracklijst gebruiken (`.txt`, `.cue` of `.json`) in plaats van MusicBrainz |
| `-o MAP` | Zelf kiezen waar de nummers komen |
| `--dry-run` | Alleen laten zien wat er zou gebeuren, niets wegschrijven |
| `--order 3,1,2` | Volgorde van de nummers in het bestand opgeven |
| `--order album` | Niet naar een andere volgorde zoeken |
| `--acoustid-key SLEUTEL` | Nummers herkennen aan hun geluid bij het uitzoeken van de volgorde |
| `--snap-window 10` | Verder zoeken naar stilte (aantal seconden, standaard 5) |
| `--silence-db -30` | Grens voor "stilte" (standaard -40; -30 telt ook zachte ruis als stilte) |
| `--no-snap` | Niet naar stilte zoeken, precies op de officiële lengtes knippen |
| `--no-cover` | Geen albumhoes ophalen |
| `--cover-size 1200` | Grootte van de hoes: 250, 500 (standaard), 1200 of `full` |
| `--format md,csv,json` | Het overzicht ook als Excel-bestand (csv) en/of json opslaan |
| `--force` | Toch knippen als de lengte van het bestand en het album sterk verschillen |

---

## 8. Goed om te weten

- **Er gaat geen kwaliteit verloren.** Bij `.m4a` en `.mp3` wordt het geluid niet opnieuw gecodeerd, alleen geknipt. Andere formaten (bijvoorbeeld `.flac`) worden omgezet naar `.m4a` van goede kwaliteit.
- **Bestaande bestanden worden overschreven.** Draai je het programma twee keer voor hetzelfde album, dan worden de nummers in de uitvoermap vervangen.
- **Een album met meerdere cd's** moet als één bestand worden aangeleverd, met de cd's achter elkaar. De bestanden heten dan `1-01 - …`, `2-01 - …`.
- **Verzamelalbums** ("Various Artists") krijgen per nummer de juiste artiest. Je muziekspeler houdt ze bij elkaar als één album.
