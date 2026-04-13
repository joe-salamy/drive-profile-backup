# Profile Backup Summary

**Date:** 2026-04-13  
**Backup root:** `C:\Users\joesa`  
**Total eligible for upload:** 23.0 GB across 30,668 files  
**Skipped by rules:** 76 files (7.8 GB) — size limits, type exclusions, patterns  
**Excluded directories:** 186 (venv, AppData, .git, node_modules, **pycache**, etc.)

---

## Breakdown by Root Folder

| Folder                      |  Files |     Size | % of Total |
| --------------------------- | -----: | -------: | ---------: |
| Documents                   | 13,812 |  20.7 GB |      90.1% |
| .cache                      |  3,203 | 806.5 MB |       3.4% |
| .local                      |      3 | 685.6 MB |       2.9% |
| .vscode                     | 12,864 | 374.3 MB |       1.5% |
| .azure-functions-core-tools |    203 | 179.5 MB |       0.8% |
| .copilot                    |    114 | 114.6 MB |       0.5% |
| Pictures                    |    203 | 112.9 MB |       0.5% |
| .docker                     |     62 |  72.3 MB |       0.3% |
| .azcopy                     |     59 |   3.9 MB |      <0.1% |
| All other (27 folders)      |    145 |    ~2 MB |      <0.1% |

**Key takeaway:** Documents is 90% of the backup. `.cache`, `.local`, and `.vscode` are the next biggest — consider whether you need those backed up (see recommendations below).

---

## Breakdown by File Type

| Extension      |  Files |     Size | % of Total |
| -------------- | -----: | -------: | ---------: |
| .wav           |     95 |  11.9 GB |      51.8% |
| .m4a           |    130 |   4.9 GB |      21.1% |
| .pdf           |    888 |   1.3 GB |       5.6% |
| (no extension) |  7,508 | 754.5 MB |       3.2% |
| .dll           |    262 | 544.8 MB |       2.3% |
| .zip           |     13 | 378.4 MB |       1.5% |
| .ort           |      1 | 335.7 MB |       1.4% |
| .pptx          |     39 | 331.4 MB |       1.3% |
| .dta           |     12 | 277.3 MB |       1.1% |
| .pt            |      2 | 210.6 MB |       0.9% |
| .jsonl         |    188 | 184.9 MB |       0.8% |
| .json          |  3,618 | 180.8 MB |       0.7% |
| .js            |    159 | 177.1 MB |       0.7% |
| .jpg           |     72 | 106.5 MB |       0.4% |
| .epub          |      9 |  92.4 MB |       0.4% |
| .docx          |    498 |  91.0 MB |       0.4% |
| .txt           |  1,724 |  65.8 MB |       0.3% |
| .png           |    318 |  60.9 MB |       0.3% |
| .wasm          |     25 |  60.0 MB |       0.3% |
| .pyi           |  9,264 |  22.3 MB |       0.1% |
| All other      | ~5,500 |  ~850 MB |      ~3.5% |

**Key takeaway:** Audio files (.wav + .m4a) are 73% of the backup — almost entirely law school lecture recordings. PDFs are the next biggest category.

---

## Top 25 Largest Files

|   # |     Size | File                                                                               |
| --: | -------: | ---------------------------------------------------------------------------------- |
|   1 | 335.7 MB | Documents/law-school-python/room-reserver/.../vti-b-p32-visual.quant.ort           |
|   2 | 265.9 MB | .cache/selenium/chrome/.../chrome.dll                                              |
|   3 | 250.4 MB | Documents/Net Present AI/Miller Ink/Zips/Work Product (1).zip                      |
|   4 | 240.5 MB | .local/share/claude/versions/2.1.92                                                |
|   5 | 239.9 MB | .local/share/claude/versions/2.1.91                                                |
|   6 | 238.6 MB | .local/share/claude/versions/2.1.89                                                |
|   7 | 206.2 MB | Documents/Law school/Quant Methods/.../audio/QM 1-14.wav                           |
|   8 | 199.0 MB | Documents/law-school-python/room-reserver/edge_selenium_profile/...crx_cache/...   |
|   9 | 195.2 MB | Documents/Law school/Quant Methods/.../audio/Quant Methods 2-25.wav                |
|  10 | 191.2 MB | Documents/Law school/Quant Methods/.../audio/Quant Methods 3-2.wav                 |
|  11 | 189.7 MB | Documents/Law school/Quant Methods/.../audio/QM 2-2.wav                            |
|  12 | 184.2 MB | Documents/Law school/Quant Methods/.../audio/Quant Methods 3-4.wav                 |
|  13 | 183.4 MB | Documents/Law school/Quant Methods/.../audio/QM 2-18.wav                           |
|  14 | 177.3 MB | Documents/Law school/Quant Methods/.../audio/QM 2-11.wav                           |
|  15 | 175.9 MB | Documents/Law school/Quant Methods/.../audio/Quant Methods 3-16.wav                |
|  16 | 172.7 MB | Documents/Law school/Quant Methods/.../audio/QM 2-9.wav                            |
|  17 | 171.4 MB | Documents/Law school/Quant Methods/.../audio/Quant methods 1-21.wav                |
|  18 | 170.0 MB | Documents/Law school/Quant Methods/.../audio/QM 2-4.wav                            |
|  19 | 168.7 MB | Documents/Law school/z Archive/1L Fall/LRW/.../Intro to BB.pptx                    |
|  20 | 161.9 MB | Documents/Law school/Quant Methods/.../audio/QM 1-26.wav                           |
|  21 | 159.5 MB | Documents/Law school/Quant Methods/.../audio/Quant Methods 3-9.wav                 |
|  22 | 156.9 MB | Documents/Law school/Quant Methods/Weeks/10/PS9/CC/Family law division 2017-26.dta |
|  23 | 156.6 MB | Documents/Law school/Quant Methods/.../audio/Quant Methods 4-6.wav                 |
|  24 | 151.2 MB | Documents/Law school/LRW/.../audio/LRW 2-6.wav                                     |
|  25 | 150.9 MB | Documents/Law school/Quant Methods/.../audio/Quant Methods 4-1.wav                 |

**18 of the top 25 are lecture .wav recordings.** The rest are a Selenium browser cache, Claude Code binaries, a zip file, an ML model (.ort), a PowerPoint, and a Stata dataset.

---

## Recommendations

Things to consider adding to your exclusions before the first real backup:

1. **`.local/share/claude/versions/`** (719 MB) — Old Claude Code binaries. Three versions taking 719 MB. These are auto-downloaded and don't need backup. Consider adding `.local` to `exclude_dirs`.

2. **`.cache/selenium/`** — Contains browser binaries (chrome.dll at 266 MB) that are auto-downloaded by Selenium. Consider adding `.cache` to `exclude_dirs`.

3. **`.vscode/extensions/`** (374 MB, 12,864 files) — VS Code extensions are re-installable. Consider adding `.vscode` to `exclude_dirs`.

4. **`.dll` files** (545 MB) — Mostly in .cache and .vscode. If you exclude those dirs, this drops to near zero. Otherwise consider adding `".dll": 0` to `size_limits_by_type`.

5. **Selenium edge profile** — `Documents/law-school-python/room-reserver/edge_selenium_profile/` contains browser cache files (199 MB component_crx_cache, 335 MB .ort model). Consider adding `edge_selenium_profile` to `exclude_dirs`.

6. **`.azure-functions-core-tools`** (180 MB), **`.copilot`** (115 MB), **`.docker`** (72 MB) — Dev tool caches, all re-downloadable.

Excluding all of these would drop the backup from **23 GB to ~21 GB**, and cut file count by ~16,000 (mostly .pyi type stubs from .vscode).
