# Timestral licence terms: research

Status: research only (issue #795, step 1 of the parked spike #765). No dependency, model file or code was added. This is a reading of licence texts, not legal advice, and it decides nothing for the owner. No personal financial data appears here.

## Sources read

All sources were retrieved on **2026-11-25** from the primary pages, not from summaries.

| Key | What | Source URL | Revision read | LICENSE sha256 |
|---|---|---|---|---|
| [CODE] | Timestral inference code (`timestral` package) | https://github.com/mistralai/timestral/blob/a534bdbd244d64f3d9d743f46c7bf6fc330ed4ff/LICENSE | commit `a534bdbd244d64f3d9d743f46c7bf6fc330ed4ff` (`main`, the only branch) | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` |
| [CODE-README] | Code repository README, License section | https://github.com/mistralai/timestral/blob/a534bdbd244d64f3d9d743f46c7bf6fc330ed4ff/README.md | same commit | n/a |
| [W5] | Timestral-5M weights | https://huggingface.co/mistralai/Timestral-5M/blob/bdd20cfc15a3fea0e4cb5ccef37f8d3ca7aa335f/LICENSE | `bdd20cfc15a3fea0e4cb5ccef37f8d3ca7aa335f` | `648194727930df730890e9f9affdbcc5c930d406d6829559b25ca3d5a973fc8c` |
| [W28] | Timestral-28M weights | https://huggingface.co/mistralai/Timestral-28M/blob/49b26409ac87b5935539a5d8ffe7f397b720db47/LICENSE | `49b26409ac87b5935539a5d8ffe7f397b720db47` | `648194727930df730890e9f9affdbcc5c930d406d6829559b25ca3d5a973fc8c` |
| [W123] | Timestral-123M weights | https://huggingface.co/mistralai/Timestral-123M/blob/5a147287addda85f6acd8da3fb8858fe9af7e8f7/LICENSE | `5a147287addda85f6acd8da3fb8858fe9af7e8f7` | `648194727930df730890e9f9affdbcc5c930d406d6829559b25ca3d5a973fc8c` |
| [W209] | Timestral-209M weights | https://huggingface.co/mistralai/Timestral-209M/blob/9874d252ab0aae16f7f7c999f98f98d6ad7d8365/LICENSE | `9874d252ab0aae16f7f7c999f98f98d6ad7d8365` | `648194727930df730890e9f9affdbcc5c930d406d6829559b25ca3d5a973fc8c` |
| [CARD] | Model cards (`README.md`) of the four weight repositories, License section | `https://huggingface.co/mistralai/Timestral-<size>/blob/<revision above>/README.md` | revisions above | n/a |
| [MIT] | Baseline: standard MIT text from the SPDX licence list | https://github.com/spdx/license-list-data/blob/ab9b0904b43db6f4b67431efd8821feb821f1c8b/text/MIT.txt | commit `ab9b0904b43db6f4b67431efd8821feb821f1c8b` | n/a |

Checks made on the fetched files:

- The four weight LICENSE files are byte-identical (same sha256). The four model cards' License sections are identical apart from the model name in the link.
- The Hugging Face API reports `"gated": false` for all four weight repositories, so no click-through terms apply beyond the LICENSE file. Each card declares `license: other`.
- The code LICENSE is byte-identical to the stock Apache License 2.0 text at https://www.apache.org/licenses/LICENSE-2.0.txt (same sha256). The repository tree at the commit above has no `NOTICE` file. `pyproject.toml` declares `license = "Apache-2.0"`.

## Correction to #765

The code is **not** under the Modified MIT licence. Only the weights are. [CODE-README] says:

> **License:** the code in this repository is licensed under [Apache 2.0](LICENSE). The Timestral model weights are released separately on the Hugging Face Hub under a [Modified MIT license](https://huggingface.co/mistralai/Timestral-209M/blob/main/LICENSE). See [License](#license).

So #765 and this issue's title ("Modified MIT licence of … code and weights") are wrong about the code. The answers below treat the code (Apache-2.0) and the weights (Modified MIT) separately.

## Weights licence, verbatim

Almost every sentence of the weights licence differs from the MIT baseline, because "this software … (the "Software")" became "the weights of this model … (the “Model”)". So the full text is quoted here exactly as fetched from [W209], with its curly quotes. The other three sizes are identical.

> Modified MIT License
>
> Attribution notice: 2026 - Mistral AI
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of the weights of this model and associated documentation files (the “Model”), to deal in the Model without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Model, and to permit persons to whom the Model is furnished to do so, subject to the following conditions:
>
> 1. The above attribution notice and this permission notice shall be included in all copies or substantial portions of the Model.
> 2. You are not authorized to exercise any rights under this license if the global consolidated monthly revenue of your company (or that of your employer) exceeds $20 million (or its equivalent in another currency) for the preceding month. This restriction in (b) applies to the Model and any derivatives, modifications, or combined works based on it, whether provided by Mistral AI or by a third party. You may contact Mistral AI (sales@mistral.ai) to request a commercial license, which Mistral AI may grant you at its sole discretion, or choose to use the Model on Mistral AI's hosted services available at https://mistral.ai/.
>
> THE MODEL IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL MISTRAL AI BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE MODEL OR THE USE OR OTHER DEALINGS IN THE MODEL.

### Differences from the standard MIT licence [MIT]

| # | Standard MIT | Timestral weights |
|---|---|---|
| D1 | Title "MIT License" | "Modified MIT License" |
| D2 | "Copyright (c) <year> <copyright holders>" | "Attribution notice: 2026 - Mistral AI" |
| D3 | "a copy of this software and associated documentation files (the "Software")", and "Software" throughout | "a copy of the weights of this model and associated documentation files (the “Model”)", and "Model" throughout |
| D4 | "The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software." | Condition 1: "The above attribution notice and this permission notice shall be included in all copies or substantial portions of the Model." |
| D5 | (none) | Condition 2, the revenue restriction, entirely new (quoted in full above) |
| D6 | "IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE" | "IN NO EVENT SHALL MISTRAL AI BE LIABLE" |

The grant itself ("to deal in the Model without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies") keeps the MIT wording apart from D3.

### Statement outside the licence

[CODE-README] and every [CARD] add a sentence that is **not** part of either LICENSE file. The card's wording:

> *You must not use this model in a manner that infringes, misappropriates, or otherwise violates any third party’s rights, including intellectual property rights*

[CODE-README]'s wording: "When using the model weights, you must not use them in a manner that infringes, misappropriates, or otherwise violates any third party's rights, including intellectual property rights." It adds no field-of-use limit that matters to forecasting a household's own cash flow.

### Drafting defects in condition 2

- **"This restriction in (b)".** The conditions are numbered 1 and 2; there is no "(b)". The only restriction in the text is condition 2, so it is the likely referent, but the cross-reference is literally broken.
- **"combined works based on it".** The restriction follows the Model into "any derivatives, modifications, or combined works based on it, whether provided by Mistral AI or by a third party". The licence does not define "combined works". If the planner with Timestral wired in counts as a combined work, the revenue cap reaches everyone who runs the planner with the model, not only the person who downloaded it.

## Code licence (Apache-2.0), verbatim where it differs from MIT

The code LICENSE is the unmodified Apache License 2.0, not a variant of MIT. Measured against the MIT baseline [MIT], every operative section of Apache-2.0 differs: it defines its terms, splits the grant into copyright and patent licences, ends the patent licence on patent litigation, sets four redistribution conditions instead of MIT's single notice condition, covers contributions and trademarks, and words its disclaimers and support terms differently. So every section from §1's definition of Derivative Works through §9 is quoted below exactly as fetched from [CODE]. The remaining §1 definitions ("License", "Licensor", "Legal Entity", "You", "Source", "Object", "Work", "Contribution", "Contributor") only name the parties and forms these sections use. The appendix is a how-to note, not a term.

<details>
<summary>Apache License 2.0, &sect;1 (Derivative Works) and &sect;&sect;2&ndash;9, verbatim</summary>

> "Derivative Works" shall mean any work, whether in Source or Object form, that is based on (or derived from) the Work and for which the editorial revisions, annotations, elaborations, or other modifications represent, as a whole, an original work of authorship. For the purposes of this License, Derivative Works shall not include works that remain separable from, or merely link (or bind by name) to the interfaces of, the Work and Derivative Works thereof.

> 2. Grant of Copyright License. Subject to the terms and conditions of this License, each Contributor hereby grants to You a perpetual, worldwide, non-exclusive, no-charge, royalty-free, irrevocable copyright license to reproduce, prepare Derivative Works of, publicly display, publicly perform, sublicense, and distribute the Work and such Derivative Works in Source or Object form.

> 3. Grant of Patent License. Subject to the terms and conditions of this License, each Contributor hereby grants to You a perpetual, worldwide, non-exclusive, no-charge, royalty-free, irrevocable (except as stated in this section) patent license to make, have made, use, offer to sell, sell, import, and otherwise transfer the Work, where such license applies only to those patent claims licensable by such Contributor that are necessarily infringed by their Contribution(s) alone or by combination of their Contribution(s) with the Work to which such Contribution(s) was submitted. If You institute patent litigation against any entity (including a cross-claim or counterclaim in a lawsuit) alleging that the Work or a Contribution incorporated within the Work constitutes direct or contributory patent infringement, then any patent licenses granted to You under this License for that Work shall terminate as of the date such litigation is filed.

> 4. Redistribution. You may reproduce and distribute copies of the Work or Derivative Works thereof in any medium, with or without modifications, and in Source or Object form, provided that You meet the following conditions:
>
> (a) You must give any other recipients of the Work or Derivative Works a copy of this License; and
>
> (b) You must cause any modified files to carry prominent notices stating that You changed the files; and
>
> (c) You must retain, in the Source form of any Derivative Works that You distribute, all copyright, patent, trademark, and attribution notices from the Source form of the Work, excluding those notices that do not pertain to any part of the Derivative Works; and
>
> (d) If the Work includes a "NOTICE" text file as part of its distribution, then any Derivative Works that You distribute must include a readable copy of the attribution notices contained within such NOTICE file, excluding those notices that do not pertain to any part of the Derivative Works, in at least one of the following places: within a NOTICE text file distributed as part of the Derivative Works; within the Source form or documentation, if provided along with the Derivative Works; or, within a display generated by the Derivative Works, if and wherever such third-party notices normally appear. The contents of the NOTICE file are for informational purposes only and do not modify the License. You may add Your own attribution notices within Derivative Works that You distribute, alongside or as an addendum to the NOTICE text from the Work, provided that such additional attribution notices cannot be construed as modifying the License.
>
> You may add Your own copyright statement to Your modifications and may provide additional or different license terms and conditions for use, reproduction, or distribution of Your modifications, or for any such Derivative Works as a whole, provided Your use, reproduction, and distribution of the Work otherwise complies with the conditions stated in this License.

> 5. Submission of Contributions. Unless You explicitly state otherwise, any Contribution intentionally submitted for inclusion in the Work by You to the Licensor shall be under the terms and conditions of this License, without any additional terms or conditions. Notwithstanding the above, nothing herein shall supersede or modify the terms of any separate license agreement you may have executed with Licensor regarding such Contributions.

> 6. Trademarks. This License does not grant permission to use the trade names, trademarks, service marks, or product names of the Licensor, except as required for reasonable and customary use in describing the origin of the Work and reproducing the content of the NOTICE file.

> 7. Disclaimer of Warranty. Unless required by applicable law or agreed to in writing, Licensor provides the Work (and each Contributor provides its Contributions) on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied, including, without limitation, any warranties or conditions of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A PARTICULAR PURPOSE. You are solely responsible for determining the appropriateness of using or redistributing the Work and assume any risks associated with Your exercise of permissions under this License.

> 8. Limitation of Liability. In no event and under no legal theory, whether in tort (including negligence), contract, or otherwise, unless required by applicable law (such as deliberate and grossly negligent acts) or agreed to in writing, shall any Contributor be liable to You for damages, including any direct, indirect, special, incidental, or consequential damages of any character arising as a result of this License or out of the use or inability to use the Work (including but not limited to damages for loss of goodwill, work stoppage, computer failure or malfunction, or any and all other commercial damages or losses), even if such Contributor has been advised of the possibility of such damages.

> 9. Accepting Warranty or Additional Liability. While redistributing the Work or Derivative Works thereof, You may choose to offer, and charge a fee for, acceptance of support, warranty, indemnity, or other liability obligations and/or rights consistent with this License. However, in accepting such obligations, You may act only on Your own behalf and on Your sole responsibility, not on behalf of any other Contributor, and only if You agree to indemnify, defend, and hold each Contributor harmless for any liability incurred by, or claims asserted against, such Contributor by reason of your accepting any such warranty or additional liability.

</details>

What these clauses mean for this repository:

| # | Clause | Effect here |
|---|---|---|
| A1 | §1 Derivative Works excludes works that "merely link (or bind by name) to the interfaces of, the Work" | Importing `timestral` does not make the planner a Derivative Work. |
| A2 | §2 copyright grant ("reproduce, prepare Derivative Works of, publicly display, publicly perform, sublicense, and distribute") | The MIT grant's counterpart; covers use as a dependency. |
| A3 | §3 patent grant, which terminates "as of the date such litigation is filed" if You sue over patents in the Work | Not in MIT. Matters only if the owner ever brought such a suit. |
| A4 | §4 (a)–(d) redistribution conditions | Apply only when the code is redistributed. There is no NOTICE file at the commit read, so (d) does not apply today. |
| A5 | §5 contributions are under the same licence | Matters only if someone upstreams a change to Mistral. |
| A6 | §6 no trademark grant beyond "reasonable and customary use in describing the origin of the Work" | Do not use "Mistral" or "Timestral" names beyond describing the dependency. |
| A7 | §7–§8 disclaimer and limitation of liability | The MIT disclaimer's counterpart, worded differently. |
| A8 | §9 You may offer warranty or support only on Your own behalf | Not relevant to a household app. |

Apache-2.0 has no copyleft and no revenue or field-of-use limit. An MIT-licensed project may depend on it.

The loader at the commit read (`src/timestral/loading.py`, `resolve_checkpoint`) downloads only two files from the Hub: `snapshot_download(..., allow_patterns=[CONFIG_FILE, WEIGHTS_FILE])` with `config.json` and `model.safetensors`. **The weights' LICENSE file is not downloaded next to the runtime copy.**

## Answers

### 1. May this public MIT repository depend on the code and download the weights at runtime?

- **Code: yes.** Apache-2.0 allows use from an MIT project. Declaring `timestral` as a dependency redistributes nothing, and §1 keeps the planner out of "Derivative Works".
- **Weights downloaded at runtime: yes for the repository, but unclear for each person who runs it.** The repository would contain only a model id. Whoever runs the planner fetches the weights from Hugging Face and becomes the licensee. Condition 2 then decides whether that person may use them at all: "You are not authorized to exercise any rights under this license if the global consolidated monthly revenue of your company (or that of your employer) exceeds $20 million … for the preceding month." The test is about the person's company or employer, not about the app, and the text makes no exception for private or personal use. Whether a given installation (including the owner's on the basement PC) passes depends on facts this page does not and should not record.

### 2. May the weights be redistributed (for example in an image or a release artifact)?

**Yes under the licence text, with conditions.** The grant includes "copy, … publish, distribute, sublicense, and/or sell copies of the Model". The conditions:

- Condition 1: the attribution notice and the permission notice (in practice the whole LICENSE file) go with every copy.
- Condition 2 binds the distributor, and "combined works based on it" carries it to the image or artifact. Every recipient whose company or employer is over the cap still may not use it.

Redistribution is not needed for the spike, because the runtime download already works. Keeping weights out of the repository, images and release artifacts avoids redistributing them, but it does **not** avoid either condition. Condition 1 covers "all copies", which includes the runtime copy in the Hugging Face cache (see question 3). Condition 2 covers exercising "any rights under this license", and using the weights at all is such a right.

### 3. What attribution or notice is required, and where would it go?

- **Weights:** "Attribution notice: 2026 - Mistral AI" plus the permission notice, "included in all copies or substantial portions of the Model" (condition 1). Condition 1 does not distinguish redistributed copies from local ones, so read it as covering **every** copy, including the runtime copy the loader writes to the Hugging Face cache. The loader fetches only `config.json` and `model.safetensors`, so that copy lacks its LICENSE unless the integration also fetches it. Where it goes:
  - Runtime copy: fetch the repository's `LICENSE` into the same snapshot as the weights (for example by adding `LICENSE` to the download's allowed files, or a separate download of that file at the same revision), and refuse to load weights whose snapshot lacks it.
  - Any redistributed copy (image, release artifact): ship the full weights LICENSE file next to `model.safetensors`.
  - The licence says nothing about forecasts the model produces. Condition 1 names copies of the Model, not its outputs.
- **Code:** nothing while it is only a declared dependency. If the `timestral` package is bundled (for example installed in a published image), Apache §4(a) requires a copy of the Apache License to go with it. `pyproject.toml` lists `license-files = ["LICENSE"]`, so a built wheel should carry it, but this page did not build or inspect one.
- **Where in this repository:** when the spike becomes product code, a third-party notices entry (for example a "Third-party models" section in the README or a `THIRD_PARTY_NOTICES` file) naming Timestral, Apache-2.0 for the code, Modified MIT for the weights, and the revenue restriction. That way people who run this public repository learn of condition 2 before they enable the model. No such file is added in this issue.

### 4. Do any use restrictions (field of use, scale, revenue, commercial use) apply to a personal finance app?

- **Field of use:** none in either LICENSE. The README and model-card sentence about third-party rights does not limit forecasting a household's own transactions.
- **Scale:** none. Neither licence limits users, requests or deployment size.
- **Commercial use:** allowed. The weights grant includes "sell copies of the Model"; Apache-2.0 has no commercial limit.
- **Revenue: yes, condition 2.** It applies to the licensee's "company (or that of your employer)" with a $20 million monthly revenue cap, whatever the app is for. A personal finance app is not exempt. Whether it bites is **unclear** for a given person until that person's company and employer revenue is known. The broken "(b)" reference and the undefined "combined works" add to the uncertainty.

## Verdict

**Unclear:** fine with conditions (weights fetched at runtime and never committed or bundled; the weights LICENSE kept next to every copy, the runtime cache included; the Apache LICENSE with any bundled code; a notice of the revenue restriction for people who run this repository) only if condition 2 is satisfied. The owner must confirm that neither the owner's own company nor the owner's employer had global consolidated monthly revenue above $20 million in the preceding month, and decide whether to accept the broken "(b)" reference and the reach of "combined works" or ask Mistral AI (sales@mistral.ai) for clarification before the spike in #765 runs.
