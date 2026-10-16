# E2 — MapReduce (RECONSTRUCTED prompt)

**Status: reconstructed, not the original wording.** The CURATE paper (arXiv 2608.04270,
§V.A, Experiment 2) prints only "See section 5 of [35] for the full prompt" plus two bulleted
additions. In experiments 1–3, "bolded text was taken directly from [35]", but which sentences
of [35] were used is not stated, and the prototype repo (`ref/curate`, commit `3fd5c16`) contains
no experiment prompts. The original wording has been requested from the authors.

Reconstruction: the full MapReduce paragraph of [35] §5 "Benchmark Applications", verbatim,
followed by the paper's two bullets, verbatim. Math is written as in the arXiv HTML alt text.

- [35] L. Schmid et al., "SeBS-Flow: Benchmarking Serverless Cloud Function Workflows",
  EuroSys 2025. arXiv 2410.03480v3, §5, MapReduce paragraph
  (https://arxiv.org/html/2410.03480v3), retrieved 2026-10-15.

## Prompt

```
MapReduce: We base our example on prior implementations (vsw, 2024; MapReduce, 2024) and perform the standard problem of word counting. First, the split function partitions the input text into $N$ batches. $N$ parallel map functions count how often each word occurs in their text chunk next. Next, shuffle flattens the resulting array $Y_{i}|i<M$. Finally, $M$ reducers count the total occurrences of their respective word in parallel, yielding $Z_{i}$. The benchmark has two parameters: the number of mapping functions $N$, and the total number of words $W$. We set $N=3$ and $W=5000$, containing $M=5$ different words. MapReduce frameworks typically execute fully in parallel. However, the available workflow primitives necessitate the shuffle function, not relying on the array $Y_{i}$ itself but flattening it to enable the desired level of parallelism in reduce.
- For the input text generation, use [cat, dog, bird, horse, pig], each distributed evenly and shuffled randomly
- Aside from dataset generation, ensure MapReduce specific functions are fully generalizable to vocabularies of any size and frequency
```

Not included: the paper's Experiment 2 also omits "the file directory names" for brevity; none
are added here.
