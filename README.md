# CK3 Checksum

Finds which mod causes a "different checksum" error in Crusader Kings III multiplayer.

CK3 shows one checksum for all your mods together. This tool hashes each mod in a playset separately. Everyone in the lobby scans their playset, and comparing the reports shows which mods differ, which files inside them, and whether the load order matches.

## Using the exe (Windows)

Download `ck3-checksums.exe` from the [latest release](https://github.com/Haiostone/CK3-Checksum/releases/latest) and double-click it.

1. Pick **1** and choose your playset. The scan saves a `ck3-checksums-*.json` report next to the exe and opens its folder.
2. The scan ends with two hashes. If everyone's match, your mods are in sync and you can stop here.
3. Otherwise, everyone sends their report to one person.
4. That person runs the exe, picks **2**, and drags the reports into the window when asked. Selecting all the reports in Explorer and dropping them onto `ck3-checksums.exe` also works.


## Using the Python script

Needs Python 3.9 or newer, with no extra packages.

```bash
python main.py                        # menu, same as the exe (recommended)

python main.py --list                 # list your playsets
python main.py --playset "AGOT MP"    # scan a playset
python main.py test1.json test2.json  # compare reports
```

`python main.py --help` lists all options.


## What a report contains

The tool only reads files and never changes anything. A report contains:

- your name and PC name
- your Windows version
- the playset's mods and their versions
- a SHA-256 hash of every mod file

Paths inside your user folder are shortened to `~`.

## License

MIT. See [LICENSE.txt](LICENSE.txt).
