| case | strategy | requests | MB read | MB of intervals | h1-20ms-50M | h1-80ms-500M | h2-20ms-50M | openslide |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| CMU-1 level 1, all 130 chunks | per-chunk gap=64 KiB | 130 | 166.1 | 12.81 | 26.64 s | 3.79 s | 26.60 s | 252.23 s |
| CMU-1 level 1, all 130 chunks | per-chunk gap=2 B | 15496 | 12.8 | 12.81 | 52.25 s | 206.69 s | 4.37 s | 1297.38 s |
| CMU-1 level 1, all 130 chunks | per-chunk gap=opt | 776 | 158.5 | 12.81 | 25.48 s | 10.92 s | 4.33 s | 244.03 s |
| CMU-1 level 1, all 130 chunks | batched gap=2 B | 1 | 12.9 | 12.81 | 2.08 s | 0.29 s | 2.08 s | 19.93 s |
| CMU-1 level 1, all 130 chunks | batched gap=opt | 1 | 12.9 | 12.81 | 2.08 s | 0.29 s | 2.08 s | 19.93 s |
| CMU-1 level 1, all 130 chunks | multi-range batched gap=2 B | 1 | 12.9 | 12.81 | 2.08 s | 0.29 s | 2.08 s | 19.93 s |
| CMU-1 level 1, all 130 chunks | ideal (one exact request per view) | 1 | 12.9 | 12.81 | 2.08 s | 0.29 s | 2.08 s | 19.93 s |
| CMU-1 level 0, central 4×4 chunks | per-chunk gap=64 KiB | 16 | 89.2 | 1.36 | 14.29 s | 1.63 s | 14.29 s | 135.10 s |
| CMU-1 level 0, central 4×4 chunks | per-chunk gap=2 B | 2048 | 1.4 | 1.36 | 6.92 s | 27.37 s | 0.60 s | 171.58 s |
| CMU-1 level 0, central 4×4 chunks | per-chunk gap=opt | 2016 | 2.7 | 1.36 | 6.86 s | 1.68 s | 0.60 s | 129.69 s |
| CMU-1 level 0, central 4×4 chunks | batched gap=2 B | 512 | 1.4 | 1.36 | 1.79 s | 6.89 s | 0.32 s | 43.51 s |
| CMU-1 level 0, central 4×4 chunks | batched gap=opt | 510 | 1.4 | 1.36 | 1.79 s | 0.43 s | 0.32 s | 33.97 s |
| CMU-1 level 0, central 4×4 chunks | multi-range batched gap=2 B | 6 | 1.4 | 1.36 | 0.25 s | 0.10 s | 0.25 s | 43.51 s |
| CMU-1 level 0, central 4×4 chunks | ideal (one exact request per view) | 1 | 1.4 | 1.36 | 0.25 s | 0.10 s | 0.25 s | 2.63 s |

| case | reader | requests | MB read | MB of chunk values |
|---|---|---:|---:|---:|
| CMU-1 level 1, all 130 chunks | today (gap 64 KiB) | 130 | 166.1 | 13.5 |
| CMU-1 level 1, all 130 chunks | batched gap 2 B | 1 | 12.9 | 13.5 |
| CMU-1 level 0, central 4×4 chunks | today (gap 64 KiB) | 16 | 89.2 | 1.4 |
| CMU-1 level 0, central 4×4 chunks | batched gap 2 B | 512 | 1.4 | 1.4 |
