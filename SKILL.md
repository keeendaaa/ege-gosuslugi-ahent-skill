---
name: gosuslugi-admission
description: Use when the user asks about Russian university admission chances, budget places, applicant rankings, priorities, ОВП, ВПП, ВП, конкурсные списки, согласия, «куда прохожу», Госуслуги Вузнавигатор, or requests a PDF summary. Uses current public Gosuslugi lists and stable priority cascading instead of prior-year passing scores.
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

## Budget Priority Workflow

Use this workflow whenever the user asks where they pass on budget and applicant priorities are available:

1. Resolve the exact main-campus organization ID through `find` or a supplied Gosuslugi URL.
2. Run `priorities` with the target `idApplication` before making any conclusion from raw list places.
3. Read `highestPassingPriority` and locate the result where `isHighestPassingPriority` is `true`.
4. Report both `rawPlaceAmongConsents` and `effectivePlace` so the user can see the priority effect.
5. Report `seats`, `assignedCount`, `vacancies`, the direct list URL, and `updateRange`.
6. If `hypotheticalConsent` is `true`, state explicitly that the result assumes consent is moved to this university.
7. Describe the result as current, not guaranteed, and identify material snapshot or status uncertainty.

Do not manually subtract applicants based only on their numeric priority. A person visible at priority `4` leaves that group only if the complete cascade proves that they obtain a place at priorities `1` through `3`.

## Official Priority Semantics

Priority `1` is the highest. The number increases as preference decreases.

- `ОВП`, основной высший приоритет: the highest priority where an applicant passes without considering consent.
- `ВПП`, высший проходной приоритет: the highest priority where a consented applicant passes; enrollment occurs here.
- `ВП`: a common UI abbreviation for a highest priority.
- `не ВП`: the entry remains visible but the applicant is currently assigned higher.
- `НП`: not a nationally standardized abbreviation; inspect the university legend before interpreting it.

Budget consent applies to every KCP competition group in the selected university, not to one program. A person can occupy no more than one budget place and is enrolled at their VPP. Consent must be withdrawn before it is submitted to another university; there is no nationwide automatic ordering of universities.

These rules follow clauses 72, 94, 96, and 97 of Ministry Order No. 821, amended for 2026 by Order No. 905:

- http://publication.pravo.gov.ru/document/0001202411290031
- http://publication.pravo.gov.ru/document/0001202511280124

## Stable Assignment Algorithm

The command implements applicant-proposing deferred acceptance:

1. Every participating applicant proposes to their highest active priority.
2. Each group tentatively retains its strongest applicants up to `numberPlaces` using official `rating`.
3. Rejected applicants propose to their next priority.
4. Groups retain the strongest set again and reject overflow.
5. Repeat until no applicant can move.

The final assignment must satisfy:

- group capacity is never exceeded;
- each applicant holds at most one budget place;
- every assigned applicant has active or hypothetical consent in the university;
- every applicant receives the highest group available under their submitted priorities;
- no lower-ranked applicant blocks a stronger applicant who wants the group and is not assigned higher.

## API Fields And Statuses

Use the fields as follows:

- `idApplication`: join one application across organization competition lists;
- `priority`: applicant preference, ascending from `1`;
- `rating`: authoritative within-group order; prefer it over rebuilding order from scores;
- `consent`: `ONLINE` and `OFFLINE` are active, `NONE` is inactive;
- `statusName`: workflow eligibility state;
- `withoutTests`: BVI-like admission category;
- `sumMark` and `result1`...`result8`: score and exam-priority fallback data;
- `paidContract`: paid-contract state, not budget consent;
- `updateDate`: source snapshot time.

The current simulator excludes:

- `Конкурсная группа исключена`;
- `Вуз отклонил выбор конкурсной группы`;
- `Ожидаются результаты испытаний`;
- `Отказ от зачисления`;
- `Вы не прошли по конкурсу`.

It includes the target application with `NONE` as a hypothetical consent but never does this for competitors. It rejects duplicate budget priorities rather than inventing an ordering. If `rating` is absent, API array position is the fallback.

## Priority Output

Interpret organization fields:

- `highestPassingPriority`: calculated VPP, or `null` when no group can hold the target;
- `assignedCompetitionId`: group holding the target;
- `hypotheticalConsent`: whether target consent was injected;
- `competitionGroups`, `simulatedApplicants`, `assignedApplicants`: model scope;
- `updateRange`: oldest and newest timestamps across fetched lists;
- `skippedStatuses`: excluded source records.

Interpret each target result:

- `rawRating`: source list position before consent filtering;
- `rawPlaceAmongConsents`: conservative independent-list place;
- `effectivePlace`: place after stable cascading, populated only for the assigned VPP group;
- `assignedCount`: final modeled occupancy;
- `vacancies`: current modeled free seats;
- `cutoffRawRating`: source rating of the last retained applicant;
- `isHighestPassingPriority`: identifies the VPP row.

Never call a lower-priority row passing merely because the target would beat its cutoff. If the target is assigned higher, it does not occupy the lower group.

## Priority Limitations

The public data can produce a strong current main-budget estimate but not a guaranteed legal enrollment result. Explicitly disclose relevant limitations:

- quota pools and their later transfers are not modeled;
- internal-exam applicants awaiting results are excluded until results appear;
- hidden BVI categories, preferential rights, and final tie fields may be absent;
- list timestamps can differ while the sequential snapshot is downloaded;
- future consent, withdrawal, application, and result changes trigger a new cascade;
- cross-university movement is controlled by the applicant's single consent, not by this algorithm;
- paid admission uses contracts and may allow multiple priorities, so budget deferred acceptance does not apply.

Use `updateRange` to assess snapshot consistency. If exact ties or missing authoritative fields affect the boundary, report uncertainty instead of inventing a tie-break.

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
