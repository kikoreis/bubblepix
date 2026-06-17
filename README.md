bubblepix is a CLI tool for tracking, deduping and tagging images and videos.

I built bubblepix because as a family we had collected hundreds of thousands of
pictures over the years, taken with phones and digital cameras. Finding
pictures we wanted in a sea of media had become nearly impossible, as was
managing related and duplicate images collected through phone backups, Dropbox
uploads and social media shares.

bubblepix works by initially scanning and ingesting media from multiple
locations into a sqlite database, parsing metadata, path and filename. Once
in the database, it allows you to query, dedupe and then tag the images based
on criteria you specify. Tagging isn't done yet so just imagine that part :-)

bubblepix uses phash and CNN (from idealo/imagededup) for similarity matching,
which provides more comprehensive de-duplication; this helps with burst shots but
also with resizes, crops and other transformations images go through as they are
moved across apps and backup mechanisms.

# Requirements

bubblepix was created and tested with Ubuntu 26.04 LTS, and uses:

- Python 3.11+
- ffprobe for video metadata extraction
- Python libs managed with `venv` and `pip install`:
    - Pillow
    - imagehash
    - scikit-learn
    - imagededup
    - tqdm
    - rich
- exiftool (optional) for better camera model detection for Samsung videos
- feh (optional) for visual duplicate comparison in `dedup review`

# Quickstart

```
# ensure ffprobe is available
sudo apt install ffmpeg # or distro equivalent

# setup local package
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# pull in images from ingest and archive folders
bubblepix catalog build --ingest ~/Uploads --archive ~/Images 

# find near-duplicates (phash + CNN)
bubblepix dedup find

# review duplicates interactively; install feh for a visual comparison
bubblepix dedup review

# query catalog
bubblepix catalog query --where "exif_date IS NULL" --limit 10

# learn what else is there
bubblepix --help
```

# An example

A catalog import on my images takes a bit over 2h on my 14-thread X1 Carbon:

```
Processing: 100%|█████████████| 290755/290755 [2:16:51<00:00, 35.41files/s]

Catalog: /home/kiko/.bubblepix/catalog.db
   290,755 files  (1040.9 GB)
   290,755 new         0 updated
   287,706 with EXIF date
     3,049 without date (orphans)
     2,504 duplicate groups

real    137m14,962s
user    3m12,193s
sys     1m18,343s
```

