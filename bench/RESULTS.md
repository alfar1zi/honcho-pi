# Benchmark Results

## What was measured

Measured module: `src/omp_memory/db.py`
Run date: 2026-11-13 (from `date -I`)

Module MD5 across all three runs:
- Python 3.10 (3.10.21): `e2382e6e50d24bab0287e7255dde8d1f`
- Python 3.12 (3.12.3): `e2382e6e50d24bab0287e7255dde8d1f`
- Python 3.13 (3.13.15): `e2382e6e50d24bab0287e7255dde8d1f`

The hash is identical across all three interpreter runs.
Note: The harness directly measures the db.py file designated by `module_md5`, not git repository state.

## Group A: dedup

Variant set: 6 variants written (base, casing, whitespace, source, project, paraphrase).

| Mode | Python | units_for_one_fact | dedup_ratio | source_values | project_provenance |
|---|---|---|---|---|---|
| upsert_record | 3.10.21 | 6 | 1.0 | ['s1', 's2'] | ['', '', '', '', '', ''] |
| upsert_record | 3.12.3 | 6 | 1.0 | ['s1', 's2'] | ['', '', '', '', '', ''] |
| upsert_record | 3.13.15 | 6 | 1.0 | ['s1', 's2'] | ['', '', '', '', '', ''] |
| remember | 3.10.21 | 2 | 0.3333 | ['s1,s2', 's1'] | ['p1', 'p1,p2'] |
| remember | 3.12.3 | 2 | 0.3333 | ['s1,s2', 's1'] | ['p1', 'p1,p2'] |
| remember | 3.13.15 | 2 | 0.3333 | ['s1,s2', 's1'] | ['p1', 'p1,p2'] |
| remember_upsert | 3.10.21 | 2 | 0.3333 | ['s1,s2', 's1'] | ['p1', 'p1,p2'] |
| remember_upsert | 3.12.3 | 2 | 0.3333 | ['s1,s2', 's1'] | ['p1', 'p1,p2'] |
| remember_upsert | 3.13.15 | 2 | 0.3333 | ['s1,s2', 's1'] | ['p1', 'p1,p2'] |

## Group B: retrieval

Retrieval API: `recall`, top k = 5, 4 queries per condition.

| Corpus (N) | Query Kind | Python | R@5 | hit@1 | MRR | p95 (ms) |
|---|---|---|---|---|---|---|
| 200 | paraphrase | 3.10.21 | 1.0 | 1.0 | 1.0 | 0.222 |
| 200 | paraphrase | 3.12.3 | 1.0 | 1.0 | 1.0 | 0.200 |
| 200 | paraphrase | 3.13.15 | 1.0 | 1.0 | 1.0 | 0.210 |
| 200 | specific | 3.10.21 | 1.0 | 1.0 | 1.0 | 0.248 |
| 200 | specific | 3.12.3 | 1.0 | 1.0 | 1.0 | 0.236 |
| 200 | specific | 3.13.15 | 1.0 | 1.0 | 1.0 | 0.239 |
| 2000 | paraphrase | 3.10.21 | 1.0 | 1.0 | 1.0 | 0.354 |
| 2000 | paraphrase | 3.12.3 | 1.0 | 1.0 | 1.0 | 0.342 |
| 2000 | paraphrase | 3.13.15 | 1.0 | 1.0 | 1.0 | 0.345 |
| 2000 | specific | 3.10.21 | 1.0 | 1.0 | 1.0 | 0.449 |
| 2000 | specific | 3.12.3 | 1.0 | 1.0 | 1.0 | 0.438 |
| 2000 | specific | 3.13.15 | 1.0 | 1.0 | 1.0 | 0.435 |

## Group C: token payload

Compact context block vs full database rows (5 rows). Identical across Python 3.10, 3.12, 3.13.

| Representation | Characters | Coarse Token Estimate (chars//4) | Coarse Token Estimate (words*1.35) |
|---|---|---|---|
| full (raw rows) | 894 | 223.5 | 135.0 |
| compact (context block) | 305 | 76.2 | 40.5 |

- `compact_over_full`: 0.3
- `row_width_full`: 178.8 characters/row

## Group D: staleness

Probe for supersession and ranking between verified and unverified claims.
Identical across Python 3.10, 3.12, 3.13.

| Metric / Check | Value |
|---|---|
| columns_probed | superseded, supersedes, invalidated, valid_until, replaced_by |
| marker_columns_found | [] |
| distinct_rows | 1 |
| old_verified_id | 5d9d70d1724d9ce2 (rank 1, verified: 1) |
| new_unverified_id | 4cdb2499b80594ff (rank 2, verified: 0) |
| verified_wins | True |
| writer | remember |
| verdict | TIDAK ADA DI POHON: no supersession marker column; older verified stays live |

## Group E: integrity

Corpus validation and FTS consistency check (200 records). Identical across Python 3.10, 3.12, 3.13.

| Check | Expected | Observed | Status |
|---|---|---|---|
| quick_check | ok | ok | PASS |
| records count | 200 | 200 | PASS |
| fts count | 200 | 200 | PASS |
| records_equal_fts | True | True | PASS |
| fts_missing_ids | 0 | 0 | PASS |
| fts_orphan_ids | 0 | 0 | PASS |
| rule_rows | 60 | 60 | PASS (rule_intact: True) |
| verified_rows | 60 | 60 | PASS (verified_intact: True) |

## Limitations

- Group B mencapai R@5 = 1.0, hit@1 = 1.0, MRR = 1.0 dan win_rate = 1.0 pada SETIAP lengan. Dataset bench karena
  itu tidak membedakan kualitas rerank; angka ini membuktikan tidak ada regresi dan latensi tetap sub-milidetik,
  BUKAN bahwa rerank mengalahkan baseline.
- Hanya 3 interpreter tersedia; pytest hanya terpasang di 3.12. Di 3.10/3.13 bukti pengganti adalah
  py_compile + import + angka harness, bukan suite penuh.
- Heat dan bobot rerank adalah kebijakan tetap yang tidak di-fit; tidak ada dataset berlabel, jadi tidak ada
  klaim akurasi.
- Angka latensi berasal dari satu mesin, satu file SQLite lokal, tanpa konkurensi.
- Tidak ada perbandingan dengan sistem lain, dan dokumen ini tidak boleh dibaca sebagai klaim benchmark
  pihak ketiga.

## How to reproduce

Tiga perintah untuk mereproduksi hasil verifikasi dan pengukuran:

```bash
cd bench && python3.12 verify.py
cd .. && bash bench/run_all.sh
python3.12 -m pytest -q
```
