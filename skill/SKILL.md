---
name: stata-ai-fusion
description: "全能 Stata AI 助手。覆盖代码生成、执行、调试的完整工作流。
在用户提及 Stata、.do 文件、.dta 文件、回归分析、面板数据、生存分析、
计量经济学、因果推断、数据清理、或任何统计分析任务时触发。
即使用户没有明确提到 Stata，如果之前的对话上下文涉及 Stata 也应触发。
也适用于调试 Stata .log 文件中的错误。"
---

# Stata AI Fusion Skill

You have access to comprehensive Stata reference files. **Do not load all files.**
Read only the 1-3 files relevant to the user's current task using the routing table below.

---

## 1. Execution Mode Detection

Detect which execution mode is available and adapt accordingly.

### MCP Mode (Stata MCP Server Available)
When the MCP server is connected, you can execute Stata code directly:
- Use `stata_run_command` to run code and return results
- Use `stata_run_do_file` to run full .do files
- Use `stata_inspect_data` to inspect loaded datasets (summary stats, variable info)
- Use `stata_codebook` to generate a codebook for the current dataset
- Use `stata_get_results` to retrieve stored estimation results (`r()`, `e()`, `s()`)
- Use `stata_export_graph` to export the current Stata graph to an image
- Use `stata_search_log` to search through Stata log output
- Use `stata_install_package` to install community-contributed packages
- Use `stata_cancel_command` to cancel a running Stata command
- Use `stata_list_sessions` to list all active Stata sessions
- Use `stata_close_session` to close a specific Stata session
- Always check execution results before proceeding
- If execution fails, read `references/error-codes.md` for diagnosis

### Offline Mode (No MCP Server)
When MCP is not available, generate code for the user to run manually:
- Write complete, self-contained .do files
- Include `version`, `clear all`, `set more off` preamble
- Add `log using` for output capture
- Comment every non-obvious line
- Wrap risky operations with `capture` and check `_rc`

### Detection Logic
```
IF MCP tools are listed in available tools:
    -> Use MCP execution mode
    -> Run code incrementally, check results between steps
ELSE:
    -> Use offline code generation mode
    -> Produce complete .do file with full preamble
```

---

## 2. Core Coding Standards (Mandatory Rules)

Every piece of Stata code you generate MUST follow these rules. Violations cause silent bugs.

### Missing Values Sort to +Infinity
Stata `.` (and `.a`-`.z`) are greater than all numbers.
```stata
* WRONG -- includes observations where income is missing!
gen high_income = (income > 50000)

* RIGHT
gen high_income = (income > 50000) if !missing(income)

* WRONG -- missing ages appear in this list
list if age > 60

* RIGHT
list if age > 60 & !missing(age)
```

### `=` vs `==`
`=` is assignment; `==` is comparison. Mixing them is a syntax error or silent bug.
```stata
* WRONG -- syntax error
gen employed = 1 if status = 1

* RIGHT
gen employed = 1 if status == 1
```

### Local Macro Syntax
Locals use `` `name' `` (backtick + single-quote). Globals use `$name` or `${name}`.
Forgetting the closing quote is the number-one macro bug.
```stata
local controls "age education income"
regress wage `controls'        // correct
regress wage `controls         // WRONG -- missing closing quote
regress wage 'controls'        // WRONG -- wrong quote characters
```

### `by` Requires Prior Sort (Use `bysort`)
```stata
* WRONG -- error if data not sorted by id
by id: gen first = (_n == 1)

* RIGHT -- bysort sorts automatically
bysort id: gen first = (_n == 1)
```

### Factor Variable Notation (`i.` and `c.`)
```stata
* WRONG -- treats race as continuous
regress wage race education

* RIGHT -- creates dummies automatically
regress wage i.race education

* Interactions
regress wage i.race##c.education    // full interaction
regress wage i.race#c.education     // interaction only (no main effects)
```

### `generate` vs `replace`
`generate` creates new variables; `replace` modifies existing ones.
```stata
gen x = 1
gen x = 2          // ERROR: x already defined
replace x = 2      // correct
```

### String Comparison Is Case-Sensitive
```stata
* May miss "Male", "MALE", etc.
keep if gender == "male"

* Safer
keep if lower(gender) == "male"
```

### `merge` Always Check `_merge`
```stata
merge 1:1 id using other.dta
tab _merge                      // always inspect
assert _merge == 3              // or handle mismatches
drop _merge
```

### Line Continuation Uses `///`
```stata
regress y x1 x2 x3 ///
    x4 x5 x6, ///
    vce(robust)
```

### Stored Results: `r()` vs `e()` vs `s()`
- `r()` -- r-class commands (summarize, tabulate, etc.)
- `e()` -- e-class commands (estimation: regress, logit, etc.)
- `s()` -- s-class commands (parsing)

A new estimation command overwrites previous `e()` results. Store them first:
```stata
regress y x1 x2
estimates store model1
```

### Weights Are Not Interchangeable
- `fweight` -- frequency weights (replication)
- `aweight` -- analytic/regression weights (inverse variance)
- `pweight` -- probability/sampling weights (survey data, implies robust SE)
- `iweight` -- importance weights (rarely used)

### `preserve` / `restore` for Temporary Changes
```stata
preserve
collapse (mean) income, by(state)
* ... do something with collapsed data ...
restore   // original data is back
```

### `capture` Swallows Errors
```stata
capture some_command
if _rc != 0 {
    di as error "Failed with code: " _rc
    exit _rc
}
```

---

## 3. Workflow Templates

### Data Cleaning Pipeline
```stata
* 1. Load and inspect
import delimited "raw_data.csv", clear varnames(1)
describe
codebook, compact

* 2. Clean
rename *, lower
destring income, replace force
replace income = . if income < 0

* 3. Label
label variable income "Annual household income (USD)"
label define yesno 0 "No" 1 "Yes"
label values employed yesno

* 4. Save
compress
save "clean_data.dta", replace
```

### Regression Table Workflow
```stata
eststo clear
eststo: regress y x1 x2, vce(robust)
eststo: regress y x1 x2 x3, vce(robust)
eststo: regress y x1 x2 x3 x4, vce(cluster id)

esttab using "results.tex", replace ///
    se star(* 0.10 ** 0.05 *** 0.01) ///
    label booktabs ///
    title("Main Results") ///
    mtitles("(1)" "(2)" "(3)")
```

### Panel Data Setup
```stata
xtset panelid timevar
xtdescribe
xtsum outcome

xtreg y x1 x2, fe vce(cluster panelid)
* Or with reghdfe
reghdfe y x1 x2, absorb(panelid timevar) vce(cluster panelid)
```

### Difference-in-Differences
```stata
* Classic 2x2 DiD
gen post = (year >= treatment_year)
gen treat_post = treated * post
regress y treated post treat_post, vce(cluster id)

* Modern staggered DiD (Callaway & Sant'Anna)
csdid y x1 x2, ivar(id) time(year) gvar(first_treat) agg(event)
csdid_plot
```

### Publication-Quality Graph
```stata
twoway (scatter y x, mcolor(navy%50) msize(small)) ///
       (lfit y x, lcolor(cranberry) lwidth(medthick)), ///
    title("Title Here") ///
    xtitle("X Label") ytitle("Y Label") ///
    legend(off) scheme(plotplain)
graph export "figure1.pdf", replace as(pdf)
graph export "figure1.png", replace as(png) width(2400)
```

### Multiple Imputation
```stata
mi set mlong
mi register imputed income education
mi impute chained (regress) income (ologit) education ///
    = age i.gender, add(20) rseed(12345)
mi estimate: regress wage income education age i.gender
```

### Survival Analysis Quick Start
```stata
stset time_var, failure(event_var)
stsum
sts graph, by(group)
sts test group
stcox i.group age, vce(robust)
estat phtest, detail
```

---

## 4. Reference Document Navigation (Routing Table)

Read only the files relevant to the user's task. Paths are relative to this SKILL.md file.

### Data Operations
| File | Topics & Key Commands |
|------|----------------------|
| `references/syntax-core.md` | Variable types, operators, conditionals, loops, macros, string/numeric/date functions |
| `references/data-management.md` | import/export, merge, reshape, collapse, egen, encode/decode, append, tempfile |
| `references/defensive-coding.md` | assert, confirm, capture, isid, version, log management |

### Econometrics & Statistics
| File | Topics & Key Commands |
|------|----------------------|
| `references/econometrics.md` | OLS, IV/2SLS, GMM, Panel FE/RE, Hausman, cluster SE, robust SE, reghdfe |
| `references/causal-inference.md` | DID, Event Study, RDD, Synthetic Control, Matching, PSM, IPTW |
| `references/survival-analysis.md` | stset, KM curves, stcox, streg, competing risks, estat phtest |

### Clinical & Domain-Specific
| File | Topics & Key Commands |
|------|----------------------|
| `references/clinical-data.md` | MIMIC-IV, ICD coding, lab values, Sepsis-3, KDIGO, mi impute |

### Output & Visualization
| File | Topics & Key Commands |
|------|----------------------|
| `references/graphics.md` | twoway, graph bar/box/dot/pie, schemes, coefplot, binscatter |
| `references/tables-export.md` | esttab/estout, outreg2, putexcel, collect framework, LaTeX/Word/CSV |

### Programming & Advanced
| File | Topics & Key Commands |
|------|----------------------|
| `references/mata.md` | Mata basics, matrix ops, st_view/st_store, custom functions |
| `references/error-codes.md` | Common Stata error codes with explanations and solutions |

### Community Packages
| File | What It Does |
|------|-------------|
| `references/packages/reghdfe.md` | High-dimensional FE regression with multi-way clustering |
| `references/packages/coefplot.md` | Coefficient plots, event study plots, multi-model comparison |
| `references/packages/gtools.md` | gcollapse, gegen, greshape -- fast data manipulation |

---

## 5. MCP Tools Usage Guide

### Tool Combination Patterns

**Pattern A: Iterative Analysis**
```
1. stata_run_command("use mydata.dta, clear")  -> load data
2. stata_run_command("describe")               -> understand structure
3. stata_run_command("regress y x1 x2")        -> run model
4. Read results, decide next step
5. stata_run_command("esttab using ...")        -> export table
```

**Pattern B: Debug Workflow**
```
1. User pastes error from .log file
2. Read references/error-codes.md for the error code
3. Identify the root cause
4. stata_run_command(corrected_code)      -> verify fix
5. Explain what went wrong
```

**Pattern C: Full Pipeline**
```
1. stata_run_command("import delimited ...")  -> load raw data
2. stata_run_command("describe \n codebook")  -> inspect
3. Generate cleaning code based on inspection
4. stata_run_command(cleaning_code)           -> clean
5. stata_run_command(analysis_code)           -> analyze
6. stata_run_command(export_code)             -> export results
```

### Error Recovery in MCP Mode
When an MCP execution returns an error:
1. Parse the error code (e.g., r(111), r(198), r(601))
2. Look up in `references/error-codes.md`
3. Apply the documented fix
4. Re-execute the corrected code
5. If still failing, try alternative approaches

### Code Generation for Offline Mode
When generating .do files for users to run manually:
```stata
/* ============================================================
   Project:  [Project Name]
   Purpose:  [What this do-file does]
   Author:   AI-generated via stata-ai-fusion
   Date:     [Current Date]
   Input:    [Input files]
   Output:   [Output files]
   ============================================================ */

version 17
clear all
set more off
set maxvar 10000

* -- Set paths --
global root "[USER_PATH]"
global data "$root/data"
global output "$root/output"

log using "$output/logs/analysis_log.txt", replace text

* -- Body --
// ... analysis code ...

log close
```

---

## 6. Common Code Snippets

### Quick Summary Statistics
```stata
summarize price mpg weight, detail
tabstat price mpg, by(foreign) stat(n mean sd p25 p50 p75)
```

### Winsorize at 1%/99%
```stata
foreach var of varlist income wealth {
    quietly summarize `var', detail
    replace `var' = r(p1) if `var' < r(p1) & !missing(`var')
    replace `var' = r(p99) if `var' > r(p99) & !missing(`var')
}
```

### Create Balanced Panel
```stata
xtset id year
bysort id: gen n_years = _N
tab n_years
keep if n_years == [expected_T]
```

### Standardize Variables (Z-score)
```stata
foreach var of varlist x1 x2 x3 {
    quietly summarize `var'
    gen `var'_z = (`var' - r(mean)) / r(sd)
}
```

### Lag and Lead Variables in Panel
```stata
xtset id year
gen y_lag1 = L.y
gen y_lag2 = L2.y
gen y_lead1 = F.y
gen y_diff = D.y
```

### Quick Coefficient Plot
```stata
regress y x1 x2 x3 x4, vce(robust)
coefplot, drop(_cons) xline(0, lcolor(red) lpattern(dash)) ///
    title("Coefficient Estimates") scheme(plotplain)
graph export "coefplot.png", replace width(2000)
```

### Export Summary Table to Excel
```stata
putexcel set "summary.xlsx", replace sheet("Summary")
putexcel A1 = "Variable" B1 = "N" C1 = "Mean" D1 = "SD"
local row = 2
foreach var of varlist income age education {
    quietly summarize `var'
    putexcel A`row' = "`var'" B`row' = r(N) ///
        C`row' = r(mean) D`row' = r(sd)
    local ++row
}
```

### Propensity Score Matching (Quick)
```stata
logit treatment x1 x2 x3
predict pscore, pr
psmatch2 treatment, pscore(pscore) outcome(y) n(1) common
```

### Event Study Plot (Quick)
```stata
gen rel_time = year - treatment_year
forvalues k = -5/5 {
    if `k' < 0 local j = abs(`k')
    else       local j = `k'
    gen D`j'_`=cond(`k'<0,"pre","post")' = (rel_time == `k')
}
* Omit period -1 as reference
reghdfe y D*_pre D*_post, absorb(id year) vce(cluster id)
coefplot, keep(D*) vertical yline(0) xline(5.5, lpattern(dash))
```

### Check for Multicollinearity
```stata
regress y x1 x2 x3 x4
estat vif
* VIF > 10 indicates concern
```

### Robust Hausman Test (with clustered SE)
```stata
* Standard Hausman fails with robust/cluster SE. Use this instead:
xtreg y x1 x2, fe vce(cluster id)
estimates store fe
xtreg y x1 x2, re vce(cluster id)
estimates store re
hausman fe re, sigmamore
```
