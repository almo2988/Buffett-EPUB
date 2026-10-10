# Buffett Essentials EPUB

A Python script that builds **Buffett Essentials: Selected Letters**, an EPUB
for Kindle and other e-readers, from the documents Berkshire Hathaway publishes
on [berkshirehathaway.com](https://www.berkshirehathaway.com/).

This repository does not contain the book. The script downloads the text from
Berkshire's website when you run it and builds the EPUB on your computer.

## What's in the book

- [The Berkshire Hathaway Owner's Manual](https://www.berkshirehathaway.com/ownman.pdf) (current edition)
- [The Owner's Manual, January 1999 edition](https://www.berkshirehathaway.com/owners.html), which still has the Introduction and the "Purchase-Accounting Adjustments" section
- [The Owner's Manual, original June 1996 edition](https://www.berkshirehathaway.com/1996ar/manual.html), with passages found in neither later edition
- Shareholder letters for [1987](https://www.berkshirehathaway.com/letters/1987.html),
  [1989](https://www.berkshirehathaway.com/letters/1989.html),
  [2006](https://www.berkshirehathaway.com/letters/2006ltr.pdf),
  [2008](https://www.berkshirehathaway.com/letters/2008ltr.pdf) and
  [2014](https://www.berkshirehathaway.com/letters/2014ltr.pdf)

Tables are kept as tables (or as images when too wide for an e-reader screen),
footnotes are linked, and the table of contents has entries for the main
sections. After building, the script compares every chapter word by word with
its source and reports anything missing.

## Build it

Requires Python 3 (tested with 3.13).

```sh
pip install requests brotli pdfplumber pillow
python3 build_epub.py
```

The book is written to `output/Buffett_Essentials.epub` next to the script. Options:

- `--out PATH`: write the EPUB somewhere else
- `--cache DIR`: where the downloaded sources are kept (default `~/.cache/buffett-epub`)

To read it on a Kindle, use [Send to Kindle](https://www.amazon.com/sendtokindle),
which accepts EPUB files.

## Copyright

The Owner's Manual and the shareholder letters are copyrighted by Warren E.
Buffett. Berkshire Hathaway's [website terms](https://www.berkshirehathaway.com/disclaimer.html)
prohibit reproducing or distributing material from the site without written
permission, so please don't share the EPUB you build. It is for your own
reading.

This project is not affiliated with or endorsed by Warren Buffett or
Berkshire Hathaway Inc.

The code in this repository (`build_epub.py`) is released under the
[MIT License](LICENSE).
