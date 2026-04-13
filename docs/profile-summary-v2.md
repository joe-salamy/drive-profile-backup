# Profile Backup Summary (v2 -- After Exclusion Updates)

**Date:** 2026-04-13  
**Backup root:** `C:\Users\joesa`  
**Total eligible for upload:** 6.7 GB across 9,362 files  
**Skipped by rules:** 132 files (14.7 GB) -- size limits, type exclusions, path patterns, specific files  
**Skipped by errors:** 0 files  
**Excluded directories:** 141 (venv, AppData, .git, node_modules, .cache, .vscode, .local, .docker, etc.)

---

## Change Summary vs. v1

| Metric                | v1 (Before) |    v2 (After) |                            Delta |
| --------------------- | ----------: | ------------: | -------------------------------: |
| Files eligible        |      30,668 |         9,362 |                          -21,306 |
| Size eligible         |     23.0 GB |        6.7 GB |                         -16.3 GB |
| Directories excluded  |         186 |           141 |     -45 (pruned earlier in tree) |
| Files skipped (rules) | 76 (7.8 GB) | 132 (14.7 GB) | +56 files, +6.9 GB moved to skip |

**What changed (v1 -> v2):**

- **-16.3 GB** total reduction (23.0 GB --> 6.7 GB, a 71% cut)
- **-21,306 files** removed
- New directory exclusions: `.local`, `.cache`, `.vscode`, `edge_selenium_profile`, `.azure-functions-core-tools`, `.copilot`, `.docker`
- New type exclusions: `.dll`, `.ort`
- New path pattern: `*/open-law-notes/*.wav` (93 duplicate .wav files, 11.9 GB)
- 11 specific large files excluded (1.4 GB) -- zip archives, duplicate PDFs, one-off recordings, datasets, PowerPoints

---

## Breakdown by Root Folder

| Folder                 | Files |     Size | % of Total |
| ---------------------- | ----: | -------: | ---------: |
| Documents              | 8,956 |   6.6 GB |      98.3% |
| Pictures               |   203 | 112.9 MB |       1.6% |
| .azcopy                |    59 |   3.9 MB |      <0.1% |
| Calibre Library        |     6 | 586.9 KB |      <0.1% |
| .aitk                  |    49 | 251.6 KB |      <0.1% |
| .ipython               |     2 | 152.4 KB |      <0.1% |
| .drive-backup          |     3 | 137.8 KB |      <0.1% |
| .matplotlib            |     1 | 110.7 KB |      <0.1% |
| .azure                 |    39 |  89.7 KB |      <0.1% |
| (root)                 |     7 |  68.0 KB |      <0.1% |
| All other (14 folders) |    37 |  ~243 KB |      <0.1% |

**Key takeaway:** Documents is 98.3% of the backup. Everything else combined is ~120 MB.

---

## Breakdown by File Type

| Extension |  Files |     Size | % of Total |
| --------- | -----: | -------: | ---------: |
| .m4a      |    127 |   4.5 GB |      67.0% |
| .pdf      |    886 |   1.1 GB |      16.2% |
| .jsonl    |    185 | 184.5 MB |       2.7% |
| .json     |  2,366 | 144.5 MB |       2.1% |
| .pptx     |     37 | 120.4 MB |       1.8% |
| .jpg      |     69 | 106.1 MB |       1.6% |
| .epub     |      9 |  92.4 MB |       1.4% |
| .docx     |    498 |  91.0 MB |       1.3% |
| .png      |    268 |  55.6 MB |       0.8% |
| .zip      |      8 |  52.3 MB |       0.8% |
| .txt      |  1,563 |  49.3 MB |       0.7% |
| .wav      |      2 |  47.6 MB |       0.7% |
| .html     |    300 |  33.3 MB |       0.5% |
| .heic     |     10 |  32.3 MB |       0.5% |
| .md       |  1,725 |  32.0 MB |       0.5% |
| .svg      |     34 |  27.2 MB |       0.4% |
| .tif      |      3 |  24.8 MB |       0.4% |
| All other | ~1,200 |  ~120 MB |      ~1.8% |

**Key takeaway:** .m4a lecture recordings are 67% of the backup. PDFs are 16%. Together they account for 83% of all data. The duplicate .wav files and large one-off files are gone.

---

## Top 25 Largest Files

|   # |     Size | File                                                                                     |
| --: | -------: | ---------------------------------------------------------------------------------------- |
|   1 | 114.6 MB | Documents/Law school/LRW/open-law-notes/.../LRW 1-16.m4a                                 |
|   2 | 104.3 MB | Documents/Law school/LRW/open-law-notes/.../LRW 1-21.m4a                                 |
|   3 |  92.1 MB | Documents/Law school/LRW/open-law-notes/.../LRW 2-6.m4a                                  |
|   4 |  82.4 MB | Documents/Law school/Property/open-law-notes/.../Property 4-7.m4a                        |
|   5 |  63.2 MB | Documents/Law school/LRW/open-law-notes/.../LRW 1-28-edited.m4a                          |
|   6 |  51.6 MB | Documents/Law school/Quant Methods/open-law-notes/.../QM 1-14.m4a                        |
|   7 |  49.9 MB | Documents/Law school/Quant Methods/open-law-notes/.../Quant Methods 2-25.m4a             |
|   8 |  48.8 MB | Documents/Law school/Quant Methods/open-law-notes/.../Quant Methods 3-2.m4a              |
|   9 |  47.2 MB | Documents/Law school/Property/Property, 10th (DKASS).pdf                                 |
|  10 |  47.1 MB | Documents/Law school/Quant Methods/open-law-notes/.../QM 2-2.m4a                         |
|  11 |  47.0 MB | Documents/Law school/Quant Methods/open-law-notes/.../Quant Methods 3-4.m4a              |
|  12 |  46.8 MB | Documents/Law school/Quant Methods/open-law-notes/.../QM 2-18.m4a                        |
|  13 |  45.3 MB | Documents/Law school/Quant Methods/open-law-notes/.../QM 2-11.m4a                        |
|  14 |  44.9 MB | Documents/Law school/Quant Methods/open-law-notes/.../Quant Methods 3-16.m4a             |
|  15 |  44.1 MB | Documents/Law school/Quant Methods/open-law-notes/.../QM 2-9.m4a                         |
|  16 |  43.4 MB | Documents/Law school/Quant Methods/open-law-notes/.../QM 2-4.m4a                         |
|  17 |  42.9 MB | Documents/Law school/Quant Methods/open-law-notes/.../Quant methods 1-21.m4a             |
|  18 |  42.4 MB | Documents/Law school/z Archive/1L Fall/Civ pro/LLM/.../LRW 11-19.m4a                     |
|  19 |  42.4 MB | Documents/Law school/z Archive/1L Fall/Contracts/LLM/lecture-input/.../LRW 11-19.m4a     |
|  20 |  42.4 MB | Documents/Law school/z Archive/1L Fall/Contracts/LLM/lecture-processed/.../LRW 11-19.m4a |
|  21 |  40.8 MB | Documents/Law school/LRW/z Archive/WA4/V1/Joe Salamy EJ Handwritten Comments.pdf         |
|  22 |  40.8 MB | Documents/Law school/z Archive/1L Fall/Contracts/LLM/.../10_20 Output & Requirements.m4a |
|  23 |  40.7 MB | Documents/Law school/Quant Methods/open-law-notes/.../Quant Methods 3-9.m4a              |
|  24 |  40.5 MB | Documents/Law school/Quant Methods/open-law-notes/.../QM 1-26.m4a                        |
|  25 |  40.0 MB | Documents/Law school/Quant Methods/open-law-notes/.../Quant Methods 4-6.m4a              |

**All top 25 are now law school content** -- .m4a lecture recordings and one PDF. The largest file is 114.6 MB (down from 335.7 MB in v1).

---

## Errors (0 files)

No errors. The 13 files with paths >= 260 characters (Windows limit) are now handled via the `\\?\` extended-path prefix for I/O, with their filenames truncated to fit within 260 chars for the Drive upload. These are in `law-essay-gen` and `1L Fall/Civ pro`.
