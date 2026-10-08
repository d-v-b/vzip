
### CMU-1, HTTP/1.1×6, 20 ms, 50 Mbit/s

| reader | trace | views | requests | MB | measured s | model s |
|---|---|---:|---:|---:|---:|---:|
| today: per-chunk gap 64 KiB | pan_L0 | 4 | 6 | 25.5 | 4.29 | 4.15 |
| today: per-chunk gap 64 KiB | pan_mid | 4 | 12 | 13.5 | 2.42 | 2.24 |
| batched gap 2 B + span cache | pan_L0 | 4 | 768 | 2.5 | 3.09 | 2.72 |
| batched gap 2 B + span cache | pan_mid | 4 | 1024 | 1.8 | 4.15 | 3.53 |
| batched cost model + span cache | pan_L0 | 4 | 756 | 2.6 | 3.07 | 2.67 |
| batched cost model + span cache | pan_mid | 4 | 24 | 2.3 | 0.62 | 0.44 |

### CMU-1, HTTP/1.1×6, 80 ms, 500 Mbit/s

| reader | trace | views | requests | MB | measured s | model s |
|---|---|---:|---:|---:|---:|---:|
| today: per-chunk gap 64 KiB | pan_L0 | 4 | 6 | 25.5 | 0.69 | 0.65 |
| today: per-chunk gap 64 KiB | pan_mid | 4 | 12 | 13.5 | 0.60 | 0.54 |
| batched gap 2 B + span cache | pan_L0 | 4 | 768 | 2.5 | 10.77 | 10.33 |
| batched gap 2 B + span cache | pan_mid | 4 | 1024 | 1.8 | 14.60 | 13.77 |
| batched cost model + span cache | pan_L0 | 4 | 18 | 8.4 | 0.45 | 0.37 |
| batched cost model + span cache | pan_mid | 4 | 24 | 2.3 | 0.49 | 0.36 |

### Hamamatsu-1, HTTP/1.1×6, 20 ms, 50 Mbit/s

| reader | trace | views | requests | MB | measured s | model s |
|---|---|---:|---:|---:|---:|---:|
| today: per-chunk gap 64 KiB | pan_L0 | 4 | 512 | 4.7 | 2.09 | 2.13 |
| today: per-chunk gap 64 KiB | pan_mid | 4 | 1536 | 6.2 | 5.99 | 5.51 |
| batched gap 2 B + span cache | pan_L0 | 4 | 512 | 4.7 | 2.11 | 2.13 |
| batched gap 2 B + span cache | pan_mid | 4 | 1024 | 6.2 | 4.14 | 3.95 |
| batched cost model + span cache | pan_L0 | 4 | 512 | 4.7 | 2.12 | 2.13 |
| batched cost model + span cache | pan_mid | 4 | 1024 | 6.2 | 4.12 | 3.95 |

### Hamamatsu-1, HTTP/1.1×6, 80 ms, 500 Mbit/s

| reader | trace | views | requests | MB | measured s | model s |
|---|---|---:|---:|---:|---:|---:|
| today: per-chunk gap 64 KiB | pan_L0 | 4 | 512 | 4.7 | 7.26 | 6.91 |
| today: per-chunk gap 64 KiB | pan_mid | 4 | 1536 | 6.2 | 21.75 | 20.60 |
| batched gap 2 B + span cache | pan_L0 | 4 | 512 | 4.7 | 7.28 | 6.91 |
| batched gap 2 B + span cache | pan_mid | 4 | 1024 | 6.2 | 14.65 | 13.80 |
| batched cost model + span cache | pan_L0 | 4 | 12 | 101.7 | 2.39 | 1.79 |
| batched cost model + span cache | pan_mid | 4 | 24 | 41.0 | 1.27 | 0.98 |
