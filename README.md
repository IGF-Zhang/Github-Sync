# GitHub Branch → Lokales Verzeichnis Sync-Tool

Synchronisiert den Dateibaum eines bestimmten GitHub-Branches **einseitig** in einen lokalen Ordner. Das Remote-Repository hat absolute Priorität.

## Installation

```bash
pip install -r requirements.txt
```

## Verwendung

### Grundlegende Verwendung

```bash
python github_sync.py \
  --repo octocat/Hello-World \
  --branch main \
  --local-dir ./my-local-copy
```

### Mit Token (Zugriff auf private Repositories / höheres API-Ratenlimit)

```bash
# Methode 1: Über Umgebungsvariable
export GITHUB_TOKEN="ghp_xxxxxxxxxxxx"
python github_sync.py --repo owner/repo --branch main --local-dir ./dest

# Methode 2: Über Kommandozeilenparameter
python github_sync.py --token ghp_xxxxxxxxxxxx --repo owner/repo --branch main --local-dir ./dest
```

## Parameter

| Parameter | Erforderlich | Beschreibung |
|---|---|---|
| `--token` | Nein | GitHub PAT, kann auch über die Umgebungsvariable `GITHUB_TOKEN` übergeben werden |
| `--repo` | Ja | Vollständiger Repository-Name, z.B. `octocat/Hello-World` |
| `--branch` | Ja | Branch-Name, z.B. `main` |
| `--local-dir` | Ja | Pfad zum lokalen Zielordner |

## Synchronisierungsregeln

| Status | Aktion | Log-Kennzeichnung |
|---|---|---|
| Remote und lokal identisch | Überspringen | `[SKIP]` |
| Remote und lokal unterschiedlich | Herunterladen und überschreiben | `[UPDATE]` |
| Nur remote vorhanden | Herunterladen und erstellen | `[CREATE]` |
| Nur lokal vorhanden | Löschen | `[DELETE]` |

## Funktionsweise

1. Über die GitHub API wird das ZIP-Archiv des gesamten Branches heruntergeladen
2. Die lokalen Dateien werden mit den Remote-Dateien verglichen (inkl. Normalisierung von Zeilenenden)
3. Basierend auf dem Vergleich wird entschieden, ob übersprungen, aktualisiert, erstellt oder gelöscht wird
4. Anschließend werden nur lokale Dateien und leere Ordner entfernt
