# Stata Syntax Core Reference

## Table of Contents
1. [Variable Types](#variable-types)
2. [Operators](#operators)
3. [Conditional Expressions](#conditional-expressions)
4. [Loops (forvalues, foreach, while)](#loops)
5. [Macros (local and global)](#macros)
6. [String Functions](#string-functions)
7. [Numeric Functions](#numeric-functions)
8. [Date and Time Functions](#date-and-time-functions)
9. [Special Variables and Constants](#special-variables-and-constants)
10. [Programs and Ado-Files](#programs-and-ado-files)
11. [Common Errors](#common-errors)

---

## Variable Types

### Numeric Storage Types
| Type | Bytes | Range | Use Case |
|------|-------|-------|----------|
| `byte` | 1 | -127 to 100 (101-127 reserved for extended missing values) | Small integers, indicators |
| `int` | 2 | -32,767 to 32,740 | Medium integers |
| `long` | 4 | -2.1 billion to 2.1 billion | Large integers, IDs |
| `float` | 4 | +/-1.7e38 | Default; 7 digits precision |
| `double` | 8 | +/-8.9e307 | Money, datetime, high precision |

```stata
gen byte female = (gender == 2)
gen long patient_id = _n
gen double precise_value = 3.141592653589793
recast double price                        // change storage type
compress                                    // optimize all types automatically
```

### String Types
```stata
gen str20 name = "John Doe"                // fixed-length
gen strL description = "Very long text..." // variable-length (up to 2GB)
```

### Date Types
Dates are stored as numbers (days since Jan 1, 1960). Display with formats:

| Format | Unit | Example |
|--------|------|---------|
| `%td` | Days | 01jan2020 |
| `%tw` | Weeks | 2020w1 |
| `%tm` | Months | 2020m1 |
| `%tq` | Quarters | 2020q1 |
| `%ty` | Years | 2020 |
| `%tc` | Milliseconds | 01jan2020 12:30:00 |

```stata
gen date = date(date_str, "YMD")
format date %td

gen double datetime = clock(datetime_str, "YMDhms")
format datetime %tc
```

---

## Operators

### Arithmetic
| Operator | Description |
|----------|-------------|
| `+`, `-`, `*`, `/` | Basic arithmetic |
| `^` | Exponentiation |
| `mod(a, b)` | Modulus (remainder) |

### Relational (return 1 if true, 0 if false)
| Operator | Description |
|----------|-------------|
| `==` | Equal to |
| `!=` or `~=` | Not equal to |
| `>`, `>=`, `<`, `<=` | Comparisons |

### Logical
| Operator | Description |
|----------|-------------|
| `&` | AND |
| `|` | OR |
| `!` or `~` | NOT |

### Precedence (highest to lowest)
1. `()` parentheses
2. `!`, `~`, unary `-`
3. `^`
4. `*`, `/`
5. `+`, `-`
6. `>`, `>=`, `<`, `<=`, `==`, `!=`
7. `&`
8. `|`

**Critical:** `&` binds tighter than `|`. Always use parentheses:
```stata
gen flag = (age > 18 & income > 50000) | (student == 1)
```

### String Operator
```stata
gen fullname = firstname + " " + lastname   // concatenation
```

---

## Conditional Expressions

### if Qualifier (row-level)
```stata
summarize income if age > 25
regress y x1 x2 if year >= 2010
gen young = 1 if age < 30
```

### if Command (program-level)
```stata
if _N > 100 {
    display "Large dataset"
}

if "`method'" == "ols" {
    regress y x1 x2
}
else if "`method'" == "iv" {
    ivregress 2sls y (x1 = z1)
}
else {
    display as error "Unknown method"
    exit 198
}
```

### cond() Function (Inline Conditional)
```stata
gen category = cond(age < 18, "Minor", cond(age < 65, "Adult", "Senior"))
gen abs_val = cond(x >= 0, x, -x)
```

### inlist() and inrange()
```stata
keep if inlist(state, "CA", "NY", "TX", "FL")
keep if inlist(year, 2018, 2019, 2020)
keep if inrange(age, 18, 65)

* inlist: up to 10 string args or 250 numeric args
* inrange: inclusive on both ends
```

### in Qualifier
```stata
list in 1/10                       // first 10 observations
list in -5/l                       // last 5 observations
```

---

## Loops

### forvalues (Numeric Sequences)
```stata
forvalues i = 1/10 {
    display "Iteration `i'"
}

forvalues year = 2000(5)2020 {
    display "Year: `year'"
}

forvalues i = 1(2)9 {              // odd numbers: 1, 3, 5, 7, 9
    display "`i'"
}
```

### foreach (Lists)
```stata
* Over variable names
foreach var of varlist price mpg weight {
    quietly summarize `var'
    display "`var': mean = " %9.2f r(mean)
}

* Over strings
foreach country in "USA" "Canada" "Mexico" {
    count if country == "`country'"
}

* Over local macro contents
local outcomes "income health education"
foreach outcome of local outcomes {
    regress `outcome' treatment, robust
    estimates store model_`outcome'
}

* Over numeric list
foreach val of numlist 1 5 10 25 50 {
    display "Percentile `val': " r(p`val')
}

* Over files
local files : dir "data/" files "*.dta"
foreach f of local files {
    use "data/`f'", clear
    count
}
```

### while Loop
```stata
local i = 1
while `i' <= 10 {
    display "Iteration `i'"
    local ++i
}

* Convergence check
local diff = 1
local iter = 0
while `diff' > 0.0001 & `iter' < 100 {
    * ... iterative computation ...
    local ++iter
}
```

### Nested Loops
```stata
foreach outcome in wage employment {
    forvalues year = 2010/2020 {
        regress `outcome' treatment if year == `year', robust
    }
}
```

### Loop Control
```stata
foreach var of varlist x1 x2 x3 {
    capture confirm numeric variable `var'
    if _rc {
        continue                   // skip non-numeric variables
    }
    summarize `var'
}
```

---

## Macros

### Local Macros
Exist only in the current program or interactive session.
```stata
local n = 100
local controls "age education income"
local label : variable label price

* Reference with backtick-quote
display "N = `n'"
regress wage `controls'

* Arithmetic in macros
local result = 2 + 3
local doubled = `n' * 2

* Increment
local i = 1
local ++i                         // now i = 2

* Extended macro functions
local count : word count `controls'    // 3
local first : word 1 of `controls'     // age
local upper = strupper("`controls'")
```

### Global Macros
Persist throughout the session. Use sparingly.
```stata
global datadir "/home/user/data"
global controls "age education income"

use "$datadir/mydata.dta", clear
regress wage $controls

* Clear when done
macro drop datadir controls
```

### Stored Results as Macros
```stata
summarize income
local mean_income = r(mean)
local sd_income = r(sd)
local n_income = r(N)

regress y x1 x2
local r2 = e(r2)
local nobs = e(N)
```

### Common Macro Patterns
```stata
* Build variable lists dynamically
local varlist ""
forvalues i = 1/10 {
    local varlist "`varlist' x`i'"
}
regress y `varlist'

* Date stamp
local today = string(date(c(current_date), "DMY"), "%tdCCYY-NN-DD")
save "data_`today'.dta", replace
```

---

## String Functions

### Basic String Operations
```stata
gen upper_name = strupper(name)
gen lower_name = strlower(name)
gen proper_name = strproper(name)
gen trimmed = strtrim(name)            // trim both sides
gen ltrimmed = strltrim(name)
gen rtrimmed = strrtrim(name)
```

### Substring and Length
```stata
gen first3 = substr(name, 1, 3)
gen last3 = substr(name, -3, 3)
gen len = strlen(name)
gen ulen = ustrlen(name)              // Unicode-aware length
```

### Search and Replace
```stata
gen has_dr = strpos(name, "Dr.") > 0
gen cleaned = subinstr(name, ",", "", .)        // remove all commas
gen replaced = subinstr(name, "old", "new", 1)  // replace first occurrence
```

### Regular Expressions
```stata
* Match
gen is_email = regexm(text, "[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

* Extract
gen domain = regexs(0) if regexm(email, "@(.+)$")

* Replace (Stata 14+)
gen cleaned = ustrregexra(text, "[^a-zA-Z0-9 ]", "")
```

### Split and Word Functions
```stata
split fullname, parse(" ") gen(name_part)      // creates name_part1, name_part2, ...
gen word1 = word(sentence, 1)
gen word_count = wordcount(sentence)
```

### Type Conversion
```stata
gen str_num = string(numeric_var)
gen str_formatted = string(price, "%9.2f")
destring str_var, replace
destring str_var, replace force                 // non-numeric become missing
gen num_val = real(str_var)                     // string to numeric
```

---

## Numeric Functions

### Rounding
```stata
gen r1 = round(x)                     // nearest integer
gen r2 = round(x, 0.01)              // nearest 0.01
gen r3 = floor(x)                     // round down
gen r4 = ceil(x)                      // round up
gen r5 = int(x)                       // truncate toward zero
```

### Mathematical
```stata
gen lnx = ln(x)                       // natural log
gen log10x = log10(x)                 // base-10 log
gen expx = exp(x)                     // exponential
gen sqrtx = sqrt(x)                   // square root
gen absx = abs(x)                     // absolute value
gen modx = mod(x, 10)                 // remainder
gen signx = sign(x)                   // -1, 0, or 1
gen minxy = min(x, y)                // element-wise min
gen maxxy = max(x, y)                // element-wise max
```

### Statistical
```stata
gen z = (x - 50) / 10                 // manual z-score
gen p = normal(z)                     // CDF of standard normal
gen z_from_p = invnormal(p)           // inverse normal
gen chi2_p = chi2tail(df, chi2_stat)  // chi-squared p-value
gen t_p = ttail(df, t_stat)          // t-distribution p-value
```

### Random Numbers
```stata
set seed 12345
gen u = runiform()                     // uniform [0,1)
gen n = rnormal()                      // standard normal
gen n2 = rnormal(100, 15)            // normal with mean=100, sd=15
gen pois = rpoisson(5)               // Poisson with lambda=5
gen binom = rbinomial(10, 0.3)       // Binomial(10, 0.3)
```

---

## Date and Time Functions

### Parsing Strings to Dates
```stata
gen date = date(date_str, "YMD")       // "2020-01-15"
gen date = date(date_str, "DMY")       // "15/01/2020"
gen date = date(date_str, "MDY")       // "01/15/2020"
format date %td

gen double dt = clock(datetime_str, "YMDhms")
format dt %tc
```

### Extracting Components
```stata
gen yr = year(date)
gen mo = month(date)
gen dy = day(date)
gen dow = dow(date)                    // 0=Sun, 1=Mon, ..., 6=Sat
gen wk = week(date)
gen qtr = quarter(date)
```

### Date Arithmetic
```stata
gen days_between = date2 - date1
gen next_month = date + 30
gen years_diff = (date2 - date1) / 365.25
```

### Conversion Between Date Types
```stata
gen monthly = mofd(daily_date)         // daily to monthly
format monthly %tm
gen daily_from_m = dofm(monthly)       // monthly to daily (1st of month)

gen quarterly = qofd(daily_date)       // daily to quarterly
format quarterly %tq
```

### Creating Dates from Components
```stata
gen date = mdy(month, day, year)
format date %td
```

---

## Special Variables and Constants

### System Variables
| Variable | Meaning |
|----------|---------|
| `_N` | Total observations (or in by-group) |
| `_n` | Current observation number (or within by-group) |
| `_rc` | Return code of last `capture` |
| `_pi` | 3.14159... |

### c() System Values
```stata
display c(current_date)               // "18 Feb 2026"
display c(current_time)               // "14:30:00"
display c(username)                    // system username
display c(os)                         // "MacOSX", "Windows", "Unix"
display c(stata_version)              // "17.0"
display c(N)                          // number of observations
display c(k)                          // number of variables
display c(pwd)                        // current working directory
display c(maxvar)                     // maximum variables allowed
```

---

## Programs and Ado-Files

### Defining Programs
```stata
program define myprog
    version 17
    syntax varlist [if] [in] [, Robust Detail]
    marksample touse

    foreach var of local varlist {
        quietly summarize `var' if `touse', `detail'
        display as text "`var': " as result %9.2f r(mean) ///
            " (SD: " %9.2f r(sd) ")"
    }
end
```

### Programs with Return Values
```stata
program define mystats, rclass
    version 17
    syntax varname [if] [in]
    marksample touse

    quietly summarize `varlist' if `touse', detail

    return scalar mean = r(mean)
    return scalar median = r(p50)
    return scalar sd = r(sd)
    return scalar N = r(N)
    return local varname "`varlist'"
end

mystats price
display "Mean: " r(mean)
display "Median: " r(median)
```

### syntax Command
```stata
* Parse command-line arguments
syntax varlist(min=2 max=10) [if] [in] ///
    [, Level(cilevel) Robust CLuster(varname) ///
       SAVing(string) Replace]
```

### Overwriting Programs
```stata
program drop myprog                    // drop before redefining
* or
capture program drop myprog
program define myprog
    * ...
end
```

---

## Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| Missing closing quote on macro | `` `var `` instead of `` `var' `` | Always use backtick + single quote |
| `if` vs `if` qualifier | Command-level `if` vs observation-level | `if _N > 10 {}` vs `summarize x if y > 10` |
| `=` in `if` qualifier | Used `=` instead of `==` | Use `==` for comparison |
| Loop variable not expanding | Forgot backtick-quote | Use `` `var' `` not `var` |
| Wrong date format | MDY vs DMY vs YMD | Check source data format |
| Float precision loss | Using `float` for IDs or money | Use `long` for integers, `double` for precision |
| `foreach` wrong type | `of varlist` vs `in` vs `of local` | `of varlist` checks vars exist; `in` takes raw strings |
| Global macro conflict | Two programs use same global name | Use locals instead of globals |
| `strpos` returns position | Expected true/false | Use `strpos(...) > 0` for boolean |
| Date arithmetic wrong | Mixed date types (daily vs monthly) | Convert to same type first |
