# E4 — PDF + MapReduce + Visualize (verbatim prompt)

**Status: verbatim** from the CURATE paper (arXiv 2608.04270, §V.A, Experiment 4).

## Prompt

```
Extract the words from words.pdf, split the word dataset, run MapReduce, visualize the output
```

## Revisions the paper made

- Gate 1: split MapReduce into map, shuffle, reduce
- Gate 2: reuse split verbatim and use the original rank for reduce and map

## Setup the paper describes

- Starts from the catalog saved after E2 (expected: 4 reused functions — split, map, shuffle,
  reduce — and 2 new ones — extract words, visualize output). Seed:
  `curate-skills/tests/fixtures/catalog-e2-mapreduce/` (from results-log run #6/#7); import it
  into a fresh folder with `/curate-skills:curate-catalog import …` and run with `--catalog`.
- Input `words.pdf`: "a PDF with 5 words randomly distributed between 100 and 1500
  occurrences", same vocabulary size as E2 (5). The exact PDF is not published; we will
  generate one and record its word counts as the ground truth.
