# Virtualized Nikon ND2 files

`just archives-nd2` writes a `.vzip` here for each public ND2 file of
[corpus_nd2.txt](../../../../conformance/virtualize/corpus_nd2.txt), by the ND2
profile ([spec/virtualize/nd2.md](../../../../spec/virtualize/nd2.md#part-2-the-profile))
(`python -m vzip.virtualize <url> <out>`), reading the file in place over
HTTP. The archives are generated, so they are not version-controlled. The
browser implementation (`js/src/virtualize/nd2/`) produces equivalent archives, and
`design/experiments/verify_nd2_vzip.py` checked sample frames against the `nd2`
package. The archives hold only metadata and byte-range references: the
pixels stay in the original files.

| archive | source | license |
|---|---|---|
| `biad3015_*.vzip` (13) | BioImage Archive [S-BIAD3015](https://www.ebi.ac.uk/biostudies/BioImages/studies/S-BIAD3015), "Time-lapse ND2 microscopy dataset of E. coli under IPTG and Plain M9 perturbations", Rastaghi, Libutti-Núñez, Akindipe, Hashemi, Oliveira | CC BY 4.0 |
| `biad2077_373_A1.vzip` | BioImage Archive [S-BIAD2077](https://www.ebi.ac.uk/biostudies/BioImages/studies/S-BIAD2077), "Benchmark light microscopy images matching TEM images", Konishi et al. | CC0 |
| `biad1573_Bb54_1hr_rep1.vzip` | BioImage Archive [S-BIAD1573](https://www.ebi.ac.uk/biostudies/BioImages/studies/S-BIAD1573), McCausland, Jacobs-Wagner et al. | CC0 |
| `pollen_169_cropped.vzip` | Zenodo [15493140](https://zenodo.org/records/15493140), Jonatan Bustos | CC BY 4.0 |
| `zenodo_vpa002.vzip` | Zenodo [8161776](https://zenodo.org/records/8161776), Jacqui Ross | CC BY 4.0 |

The archives embed metadata from these files (dimensions, channel names, pixel
sizes) and are not covered by this repository's licenses.
