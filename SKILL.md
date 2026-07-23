---
name: gosuslugi-admission
description: Use when the user asks about Russian university admission chances, budget places, applicant rankings, конкурсные списки, согласия, «куда прохожу», Госуслуги Вузнавигатор, or requests a PDF summary for one or more universities. Uses current public Gosuslugi applicant lists instead of prior-year passing scores.
---

# Gosuslugi Admission

Evaluate current Russian university admission lists through the public API behind `gosuslugi.ru/vuznavigator`. Never substitute prior-year passing scores when current lists are available.

## Required Inputs

Establish before calculating:

- admission year;
- every available EGE subject score and individual-achievement points;
- exact university campus/city;
- budget/paid, study form, education level, quota, and subject-area constraints.

Default only when the user agrees: main budget places, full-time study, main admission stage, bachelor/basic-higher education, no quota.

## One-Command Report

Run from this skill directory:

```bash
python3 scripts/admission.py report \
  --university "Российский университет транспорта" \
  --university "МАДИ" \
  --city Москва \
  --year 2026 \
  --score "Русский язык=55" \
  --score "Математика=72" \
  --score "Информатика=70" \
  --individual 5 \
  --pdf admission.pdf \
  --json admission.json
```

Use repeated `--org-id` instead of names when IDs are known. Name search requires `--city` and excludes branches. If more than one main-campus result remains, stop and request an exact ID.

Scores accept any subject name present in Gosuslugi or a numeric subject ID. When extra EGE subjects are supplied, the script calculates a separate correct total for each competition group from its three selected exams. It does not sum every supplied subject.

## Education Levels

Automatic mode includes available:

- `2`: bachelor;
- `6`: pilot basic higher education.

This prevents pilot universities such as RUT (MIIT) from disappearing. Add `--include-specialist` only when requested, or pass repeated explicit `--education-level` values.

Study forms: `--form-id 1` full-time (default), `2` part-time, `3` correspondence.

## Find And Inspect

```bash
python3 scripts/admission.py find --query "МАДИ" --city Москва --year 2026

python3 scripts/admission.py analyze \
  --org-id 1187 \
  --year 2026 \
  --score "Русский язык=55" \
  --score "Математика=72" \
  --score "Информатика=70" \
  --individual 5

python3 scripts/admission.py priorities \
  --org-id 24 \
  --application-id 1281570 \
  --year 2026
```

Use `priorities` for budget questions where lower-priority applicants must be moved to their highest passing priority. It downloads every main-budget group in the organization, joins entries by `idApplication`, preserves the official `rating`, and runs stable deferred acceptance. The target application is included as a hypothetical consent when its current consent is `NONE`.

Do not use budget priority simulation for paid places, quota pools, or cross-university ordering. Paid contracts are independent, quota transfers need separate capacities, and budget consent selects one university rather than creating a nationwide preference order.

## Interpretation

Report exact current place, main budget places, active non-withdrawn consents, margin to the boundary, direct list URL, and API update time.

Describe results as current, not guaranteed. New consents can arrive. Use `priorities` instead of manually subtracting applicants: a lower-priority applicant can be removed only after the complete cascade proves that they pass at a higher priority.

The ranking logic:

- keeps `ONLINE` and `OFFLINE` consents;
- excludes `Конкурсная группа исключена`;
- places BVI applicants above regular scores;
- resolves equal totals by entrance-exam priority;
- reports a best/worst range for exact ties.

## Safety And Reliability

The endpoints are public and require no cookies or authorization. Requests are sequential and retry transient network, `429`, and server failures. Client-side `4xx` errors fail immediately with the API response.

Never brute-force organization IDs. If the portal returns HTML or times out after retries, wait for throttling to clear or inspect the real browser network log. Do not bypass anti-bot or authentication controls.
