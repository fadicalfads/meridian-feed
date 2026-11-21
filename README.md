# Meridian feed

Day data for the Meridian beta pages at embassytransport.co.uk/meridian1 and /meridian2.
The pages on the website are fixed shells; each one reads `m1/latest.json` or `m2/latest.json` from here when it opens.

Each engine slot overwrites `<desk>/latest.json` and also saves a dated copy, `<desk>/YYYY-MM-DD.json`.

Fields: `date`, `updated`, `sub` (line under the masthead, HTML), `tracker` (Meridian 1 only, HTML), `feed` (all posts, HTML, with the chart block inside the first post), `week` (week strip, HTML), `chart` (pair, bars, zones, levels, views, replay).

Demo-trading analysis for education. Not financial advice.
