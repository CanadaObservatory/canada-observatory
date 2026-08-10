# Canada Observatory

Open data and pipeline code for [Canada Observatory](https://canadaobservatory.ca/), an independent, non-partisan reference site of interactive charts on the state of Canada. Most indicators compare Canada with 16 OECD peer countries; all of them come from authoritative primary sources such as Statistics Canada, the OECD, the World Bank, and Environment and Climate Change Canada.

## What is in this repository

- **`data/`** — the cleaned CSV files behind the site's charts, one folder per section, each with a JSON metadata sidecar recording its source, licence, and retrieval date. Refreshed automatically as source agencies publish.
- **`pipeline/`** — the Python code that fetches and processes every series: a declarative indicator registry, one generic fetcher per source, and the chart builders.
- **`requirements.txt`** — the Python dependencies for the pipeline.

The site's page text and build configuration live in a separate private repository; this one carries what is most useful to reuse: the data and the code that produces it. Every chart on the site also offers its CSV directly.

## Reporting an error

If you spot a number that looks wrong, please open an issue here, or write to <hello@canadaobservatory.ca>. We review every report and correct confirmed mistakes. See the site's [Accuracy, Corrections & Disclaimer](https://canadaobservatory.ca/about/disclaimer.html) page for how corrections are handled.

## Licences

- **Code** (`pipeline/`): MIT (see `LICENSE`).
- **Site text and charts**: CC BY 4.0.
- **Data** (`data/`): each file remains under its source's licence, recorded in its metadata sidecar and on the site's [Data Sources](https://canadaobservatory.ca/about/data-sources.html) page. Sources include the Statistics Canada Open Licence, the Open Government Licence — Canada, and CC BY.
- **CREA MLS® HPI**: displayed on the site as charts with permission; no CREA data appears in this repository.

Canada Observatory is independent. It is not affiliated with, produced by, or endorsed by Statistics Canada or any other data provider, government, or agency whose data it uses.
