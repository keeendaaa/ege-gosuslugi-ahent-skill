#!/usr/bin/env python3
import argparse
import html
import json
import os
import re
import sys
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

import requests


BASE = "https://www.gosuslugi.ru/api"
AUTO_EDUCATION_LEVELS = (2, 6)
FORM_NAMES = {1: "Очная", 2: "Очно-заочная", 3: "Заочная"}
LEVEL_NAMES = {
    2: "Бакалавриат",
    3: "Специалитет",
    6: "Базовое высшее образование",
}
PLACE_TYPE_BUDGET = 1
PLACE_TYPE_PAID = 3
PLACE_TYPE_NAMES = {
    PLACE_TYPE_BUDGET: "Бюджетные места",
    PLACE_TYPE_PAID: "Платные места",
}
ACTIVE_CONSENTS = {"ONLINE", "OFFLINE"}
INACTIVE_PRIORITY_STATUSES = {
    "Конкурсная группа исключена",
    "Вуз отклонил выбор конкурсной группы",
    "Ожидаются результаты испытаний",
    "Отказ от зачисления",
    "Вы не прошли по конкурсу",
}
VERDICT_ORDER = {"passing": 0, "borderline": 1, "not_passing": 2}
SUBJECT_ALIASES = {
    "русский": "русский язык",
    "рус": "русский язык",
    "профильная математика": "математика",
    "профильный математика": "математика",
    "мат": "математика",
    "инфа": "информатика",
    "икт": "информатика",
    "информатика и икт": "информатика",
    "общество": "обществознание",
    "английский": "иностранный язык английский",
    "немецкий": "иностранный язык немецкий",
    "французский": "иностранный язык французский",
    "испанский": "иностранный язык испанский",
    "китайский": "иностранный язык китайский",
}


class GosuslugiError(RuntimeError):
    pass


def normalize_subject(value: str) -> str:
    normalized = re.sub(r"[^0-9a-zа-я]+", " ", value.casefold().replace("ё", "е")).strip()
    return SUBJECT_ALIASES.get(normalized, normalized)


def parse_scores(values: list[str]) -> dict[str, int]:
    scores = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Invalid score {value!r}; expected SUBJECT=SCORE")
        subject, raw_score = value.rsplit("=", 1)
        subject = subject.strip()
        try:
            score = int(raw_score)
        except ValueError as error:
            raise ValueError(f"Invalid score value in {value!r}") from error
        if not 0 <= score <= 100:
            raise ValueError(f"Score must be between 0 and 100: {value!r}")
        key = f"id:{subject}" if subject.isdigit() else normalize_subject(subject)
        scores[key] = score
    return scores


def subject_score(subject: dict, scores: dict[str, int]) -> int | None:
    return scores.get(f"id:{subject['id']}", scores.get(normalize_subject(subject["name"])))


def exam_groups(tests: list[dict]) -> list[list[dict]]:
    by_id = {test["id"]: test for test in tests}

    def root_id(test: dict) -> int:
        current = test
        seen = set()
        while current.get("replaceEntranceTestId") and current["id"] not in seen:
            seen.add(current["id"])
            parent = by_id.get(current["replaceEntranceTestId"])
            if parent is None:
                break
            current = parent
        return current["id"]

    groups = defaultdict(list)
    for test in tests:
        groups[root_id(test)].append(test)
    return sorted(groups.values(), key=lambda group: min(test["priority"] for test in group))


def candidate_results(tests: list[dict], scores: dict[str, int]) -> tuple[int, ...] | None:
    groups = exam_groups(tests)
    if len(groups) != 3:
        return None
    results = []
    for group in groups:
        passing = [
            score
            for test in group
            if (score := subject_score(test["subject"], scores)) is not None
            and score >= test["minScore"]
        ]
        if not passing:
            return None
        results.append(max(passing))
    return tuple(results)


class GosuslugiClient:
    def __init__(self, organization_id: int, year: int, attempts: int = 3):
        self.organization_id = organization_id
        self.year = year
        self.attempts = attempts
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Referer": f"https://www.gosuslugi.ru/vuznavigator/universities/{organization_id}",
                "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/150 Safari/537.36",
            }
        )

    def request_json(self, method: str, path: str, payload: dict | None = None):
        url = f"{BASE}{path}"
        last_error = None
        for attempt in range(self.attempts):
            try:
                response = self.session.request(method, url, json=payload, timeout=(10, 60))
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    preview = response.text[:300].replace("\n", " ")
                    raise GosuslugiError(f"Gosuslugi returned {response.status_code}: {preview}")
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").casefold()
                if "json" not in content_type:
                    preview = response.text[:160].replace("\n", " ")
                    raise RuntimeError(
                        f"expected JSON, got {content_type or 'unknown content type'}: {preview}"
                    )
                return response.json()
            except GosuslugiError:
                raise
            except (requests.RequestException, ValueError, RuntimeError) as error:
                last_error = error
                if attempt + 1 < self.attempts:
                    time.sleep(2**attempt)
        raise GosuslugiError(f"Gosuslugi request failed after {self.attempts} attempts: {last_error}")

    def post(self, path: str, payload: dict):
        return self.request_json("POST", path, payload)

    def get(self, path: str):
        return self.request_json("GET", path)

    def find_organizations(self, query: str, city: str | None = None) -> list[dict]:
        response = self.post(
            f"/vuz-navigator/public/v1/{self.year}/find-organizations?page=0&size=50",
            {"searchStr": query, "oksoCodes": [], "regionIds": []},
        )
        items = response.get("content", []) if isinstance(response, dict) else response
        city_query = city.casefold() if city else None
        results = []
        for item in items:
            item_city = item.get("city") or item.get("regionName") or ""
            if city_query and city_query not in item_city.casefold():
                continue
            organization_id = item.get("organizationId") or item.get("id")
            if organization_id is None:
                continue
            results.append(
                {
                    "organizationId": organization_id,
                    "name": item.get("humanReadableTitle")
                    or item.get("shortTitle")
                    or item.get("fullTitle"),
                    "fullTitle": item.get("fullTitle"),
                    "city": item_city,
                    "regionName": item.get("regionName"),
                    "parentOrgId": item.get("parentOrgId"),
                    "filial": item.get("filial"),
                    "url": f"https://www.gosuslugi.ru/vuznavigator/universities/{organization_id}",
                }
            )
        return results

    def organization(self) -> dict:
        return self.get(f"/vuz-navigator/public/v1/{self.year}/organization/{self.organization_id}")

    def catalog(self) -> list[dict]:
        return self.post(
            f"/vuz-navigator/public/v1/{self.year}/educational-programs/items?page=0&size=1000",
            {"subjectsEgeIds": [], "orgId": self.organization_id},
        )

    def details(self, education_level: int, okso_code: str) -> list[dict]:
        return self.post(
            f"/vuz-navigator/public/v1/{self.year}/educational-programs",
            {
                "educationLevelId": education_level,
                "oksoCode": okso_code,
                "organizationId": self.organization_id,
            },
        )

    def applicants(self, competition_id: int) -> dict:
        return self.get(
            f"/university-applicant-list/v1/public/{self.year}/competition/{competition_id}/applicants"
        )


def select_organization(results: list[dict], query: str) -> dict:
    main = [
        item
        for item in results
        if item.get("filial") is not True and item.get("parentOrgId") in (None, 0)
    ]
    if len(main) == 1:
        return main[0]
    if not main:
        raise GosuslugiError(f"No main-campus organization found for {query!r}")
    choices = ", ".join(
        f"{item['organizationId']} ({item.get('name')}, {item.get('city')})" for item in main
    )
    raise GosuslugiError(f"Ambiguous organization {query!r}: {choices}. Use --org-id.")


def organization_from_id(organization_id: int, year: int) -> dict:
    item = GosuslugiClient(organization_id, year).organization()
    return {
        "organizationId": organization_id,
        "name": item.get("shortTitle") or item.get("fullTitle"),
        "fullTitle": item.get("fullTitle"),
        "city": item.get("city") or item.get("regionName"),
        "regionName": item.get("regionName"),
        "parentOrgId": item.get("parentOrgId"),
        "filial": item.get("filial"),
        "url": f"https://www.gosuslugi.ru/vuznavigator/universities/{organization_id}",
    }


def resolve_organizations(names: list[str], ids: list[int], city: str | None, year: int) -> list[dict]:
    organizations = [organization_from_id(organization_id, year) for organization_id in ids]
    search_client = GosuslugiClient(0, year)
    for name in names:
        organizations.append(select_organization(search_client.find_organizations(name, city), name))
    return list({item["organizationId"]: item for item in organizations}.values())


def available_levels(
    catalog: list[dict],
    form_id: int,
    place_type_id: int = PLACE_TYPE_BUDGET,
    max_cost: int | None = None,
) -> list[int]:
    def accepted(item: dict) -> bool:
        if item.get("placeTypeId") != place_type_id:
            return False
        if item.get("stageAdmissionId") != 1:
            return False
        if item.get("educationFormId") != form_id:
            return False
        if max_cost is not None and item.get("costOfStudy", 0) > max_cost:
            return False
        return item.get("numberPlaces", 0) > 0

    return sorted(
        {item["educationLevelId"] for item in catalog if accepted(item)}
    )


def resolve_levels(
    catalog: list[dict],
    requested: list[int] | None,
    include_specialist: bool,
    form_id: int,
    place_type_id: int = PLACE_TYPE_BUDGET,
    max_cost: int | None = None,
) -> list[int]:
    if requested:
        return list(dict.fromkeys(requested))
    available = available_levels(catalog, form_id, place_type_id, max_cost)
    levels = [level for level in AUTO_EDUCATION_LEVELS if level in available]
    if include_specialist and 3 in available:
        levels.append(3)
    return levels


def relevant_groups(
    client: GosuslugiClient,
    catalog: list[dict],
    levels: list[int],
    form_id: int,
    scores: dict[str, int],
    place_type_id: int = PLACE_TYPE_BUDGET,
    max_cost: int | None = None,
) -> list[dict]:
    def accepted(item: dict) -> bool:
        if item.get("educationLevelId") not in levels:
            return False
        if item.get("placeTypeId") != place_type_id:
            return False
        if item.get("stageAdmissionId") != 1:
            return False
        if item.get("educationFormId") != form_id:
            return False
        if max_cost is not None and item.get("costOfStudy", 0) > max_cost:
            return False
        return item.get("numberPlaces", 0) > 0

    candidates = {item["id"]: item for item in catalog if accepted(item)}
    result = []
    level_codes = sorted(
        {(item["educationLevelId"], item["oksoCode"]) for item in candidates.values()}
    )
    for education_level, code in level_codes:
        for detail in client.details(education_level, code):
            if detail["id"] in candidates and candidate_results(
                detail.get("entranceTests", []), scores
            ) is not None:
                result.append(detail)
    return result


def analyze_group(
    client: GosuslugiClient,
    group: dict,
    scores: dict[str, int],
    individual: int,
    total_override: int | None,
    place_type_id: int = PLACE_TYPE_BUDGET,
    max_cost: int | None = None,
) -> dict:
    own_results = candidate_results(group.get("entranceTests", []), scores)
    if own_results is None:
        raise ValueError("group does not accept the supplied scores")
    total_score = total_override if total_override is not None else sum(own_results) + individual
    payload = client.applicants(group["id"])
    consented = [
        applicant
        for applicant in payload["applicants"]
        if applicant.get("consent") in {"ONLINE", "OFFLINE"}
        and applicant.get("statusName") != "Конкурсная группа исключена"
    ]

    def results(applicant: dict) -> tuple[float, ...]:
        return tuple(
            applicant.get(f"result{index}", 0) for index in range(1, len(own_results) + 1)
        )

    higher_total = sum(
        applicant.get("withoutTests") or applicant.get("sumMark", 0) > total_score
        for applicant in consented
    )
    same_total = [
        applicant
        for applicant in consented
        if not applicant.get("withoutTests") and applicant.get("sumMark") == total_score
    ]
    ahead = higher_total + sum(results(applicant) > own_results for applicant in same_total)
    exact_ties = sum(results(applicant) == own_results for applicant in same_total)
    best = ahead + 1
    worst = ahead + exact_ties + 1
    seats = group.get("numberPlaces") or group.get("numberPlacesPaid") or 0
    verdict = "passing" if worst <= seats else "borderline" if best <= seats else "not_passing"
    programs = group.get("programs", [])
    ids_path = "-".join(str(program["id"]) for program in programs)
    ids_query = "~".join(str(program["id"]) for program in programs)
    return {
        "competitionId": group["id"],
        "code": group["oksoCode"],
        "specialty": group["oksoName"],
        "educationLevelId": group["educationLevelId"],
        "educationLevel": group.get("educationLevelName"),
        "programs": [program["name"] for program in programs],
        "form": group["educationFormName"],
        "placeTypeId": group.get("placeTypeId", place_type_id),
        "placeTypeName": group.get("placeTypeName") or PLACE_TYPE_NAMES.get(place_type_id, "Бюджет"),
        "costOfStudy": group.get("costOfStudy"),
        "exams": [
            " / ".join(f"{test['subject']['name']} >= {test['minScore']}" for test in item)
            for item in exam_groups(group.get("entranceTests", []))
        ],
        "candidateResults": own_results,
        "totalScore": total_score,
        "seats": seats,
        "activeConsents": len(consented),
        "placeBest": best,
        "placeWorst": worst,
        "verdict": verdict,
        "margin": seats - worst if verdict == "passing" else seats - best,
        "updated": payload.get("updateDate"),
        "url": (
            "https://www.gosuslugi.ru/vuznavigator/specialties/"
            f"{group['oksoCode']}/{group['educationLevelId']}/{client.organization_id}/1/"
            f"{ids_path}/-/applicants/{group['id']}"
            f"?program={group['educationLevelId']}_{group['educationFormId']}__{ids_query}___"
        ),
    }


def analyze_organization(
    organization: dict,
    year: int,
    scores: dict[str, int],
    individual: int,
    total_override: int | None,
    requested_levels: list[int] | None,
    include_specialist: bool,
    form_id: int,
    place_type_id: int = PLACE_TYPE_BUDGET,
    max_cost: int | None = None,
) -> dict:
    client = GosuslugiClient(organization["organizationId"], year)
    catalog = client.catalog()
    levels = resolve_levels(
        catalog, requested_levels, include_specialist, form_id, place_type_id, max_cost
    )
    groups = relevant_groups(
        client, catalog, levels, form_id, scores, place_type_id, max_cost
    )
    results = [
        analyze_group(client, group, scores, individual, total_override, place_type_id, max_cost)
        for group in groups
    ]
    results.sort(
        key=lambda item: (
            VERDICT_ORDER[item["verdict"]],
            item["placeBest"] - item["seats"],
            item["placeBest"],
        )
    )
    return {
        **organization,
        "educationLevels": levels,
        "formId": form_id,
        "form": FORM_NAMES[form_id],
        "results": results,
    }


def _rank_in_group(applicants: list[dict], target: dict, group: dict) -> int:
    tests = group.get("entranceTests", [])
    groups = exam_groups(tests)

    def result_tuple(applicant: dict) -> tuple:
        return tuple(applicant.get(f"result{index}", 0) for index in range(1, len(groups) + 1))

    def sort_key(applicant: dict) -> tuple:
        if applicant.get("withoutTests"):
            return (float("inf"), result_tuple(applicant))
        return (applicant.get("sumMark", 0), result_tuple(applicant))

    ranked = sorted(applicants, key=sort_key, reverse=True)
    for index, applicant in enumerate(ranked, start=1):
        if applicant.get("idApplication") == target.get("idApplication"):
            return index
    return 0


def track_organization(
    organization: dict,
    year: int,
    application_id: int,
    place_type_id: int,
) -> list[dict]:
    client = GosuslugiClient(organization["organizationId"], year)
    catalog = client.catalog()
    groups = [
        item
        for item in catalog
        if item.get("placeTypeId") == place_type_id
        and item.get("stageAdmissionId") == 1
        and item.get("numberPlaces", 0) > 0
    ]
    matches = []
    for group in groups:
        payload = client.applicants(group["id"])
        applicants = payload.get("applicants", [])
        applicant = next(
            (item for item in applicants if item.get("idApplication") == application_id),
            None,
        )
        if applicant is None:
            continue
        place = _rank_in_group(applicants, applicant, group)
        programs = group.get("programs", [])
        matches.append(
            {
                "organizationId": organization["organizationId"],
                "organizationName": organization.get("name"),
                "competitionId": group["id"],
                "code": group["oksoCode"],
                "specialty": group["oksoName"],
                "educationLevelId": group["educationLevelId"],
                "educationLevel": group.get("educationLevelName"),
                "educationFormId": group.get("educationFormId"),
                "form": group.get("educationFormName"),
                "placeTypeId": group.get("placeTypeId"),
                "placeTypeName": group.get("placeTypeName"),
                "costOfStudy": group.get("costOfStudy"),
                "programs": [program["name"] for program in programs],
                "seats": group.get("numberPlaces", 0),
                "place": place,
                "consent": applicant.get("consent"),
                "withoutTests": applicant.get("withoutTests", False),
                "sumMark": applicant.get("sumMark"),
                "statusName": applicant.get("statusName"),
                "updated": payload.get("updateDate"),
            }
        )
    return matches


def track_application(args: argparse.Namespace) -> dict:
    organizations = resolve_organizations(args.university, args.org_id, args.city, args.year)
    matches = []
    for organization in organizations:
        matches.extend(track_organization(organization, args.year, args.application_id, args.place_type_id))
    return {
        "year": args.year,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "applicationId": args.application_id,
        "placeTypeId": args.place_type_id,
        "placeTypeName": PLACE_TYPE_NAMES[args.place_type_id],
        "matches": matches,
    }


def deferred_acceptance(
    capacities: dict[int, int],
    preferences: dict[int, list[int]],
    rankings: dict[int, dict[int, tuple[int, int]]],
) -> dict[int, list[int]]:
    held = {group_id: [] for group_id in capacities}
    next_preference = {application_id: 0 for application_id in preferences}
    queue = deque(preferences)

    while queue:
        application_id = queue.popleft()
        index = next_preference[application_id]
        if index >= len(preferences[application_id]):
            continue

        group_id = preferences[application_id][index]
        next_preference[application_id] += 1
        held[group_id].append(application_id)
        held[group_id].sort(key=rankings[group_id].__getitem__)

        if len(held[group_id]) > capacities[group_id]:
            rejected = held[group_id].pop()
            if next_preference[rejected] < len(preferences[rejected]):
                queue.append(rejected)

    return held


def simulate_priority_organization(
    organization: dict,
    year: int,
    application_id: int,
) -> dict:
    client = GosuslugiClient(organization["organizationId"], year)
    groups = {
        item["id"]: item
        for item in client.catalog()
        if item.get("placeTypeId") == PLACE_TYPE_BUDGET
        and item.get("stageAdmissionId") == 1
        and item.get("numberPlaces", 0) > 0
    }
    entries = defaultdict(list)
    rankings = {group_id: {} for group_id in groups}
    records = {}
    updates = {}
    skipped_statuses = defaultdict(int)

    for group_id in groups:
        payload = client.applicants(group_id)
        updates[group_id] = payload.get("updateDate")
        for index, applicant in enumerate(payload.get("applicants", []), start=1):
            current_id = applicant.get("idApplication")
            if current_id is None:
                continue
            status = applicant.get("statusName")
            if status in INACTIVE_PRIORITY_STATUSES:
                skipped_statuses[status] += 1
                continue
            priority = applicant.get("priority")
            if priority is None:
                continue
            if current_id != application_id and applicant.get("consent") not in ACTIVE_CONSENTS:
                continue

            entries[current_id].append((priority, group_id))
            rankings[group_id][current_id] = (applicant.get("rating") or index, index)
            records[(group_id, current_id)] = applicant

    if application_id not in entries:
        raise GosuslugiError(
            f"Application {application_id} was not found in active main-budget lists for "
            f"organization {organization['organizationId']}"
        )

    preferences = {}
    for current_id, items in entries.items():
        ordered = sorted(items)
        priorities = [priority for priority, _ in ordered]
        if len(priorities) != len(set(priorities)):
            raise GosuslugiError(f"Application {current_id} has duplicate budget priorities")
        preferences[current_id] = [group_id for _, group_id in ordered]

    capacities = {group_id: group["numberPlaces"] for group_id, group in groups.items()}
    held = deferred_acceptance(capacities, preferences, rankings)
    assignments = {
        current_id: group_id
        for group_id, application_ids in held.items()
        for current_id in application_ids
    }
    assigned_group_id = assignments.get(application_id)
    target_priorities = {group_id: priority for priority, group_id in entries[application_id]}
    target_rows = []

    for priority, group_id in sorted(entries[application_id]):
        group = groups[group_id]
        ordered_applicants = sorted(rankings[group_id], key=rankings[group_id].__getitem__)
        application_ids = held[group_id]
        record = records[(group_id, application_id)]
        programs = group.get("programs", [])
        program_ids = "-".join(str(program["id"]) for program in programs)
        program_query = "~".join(str(program["id"]) for program in programs)
        effective_place = (
            application_ids.index(application_id) + 1 if application_id in application_ids else None
        )
        cutoff_rating = rankings[group_id][application_ids[-1]][0] if application_ids else None
        target_rows.append(
            {
                "priority": priority,
                "competitionId": group_id,
                "code": group.get("oksoCode"),
                "specialty": group.get("oksoName"),
                "educationLevelId": group.get("educationLevelId"),
                "educationLevel": group.get("educationLevelName"),
                "form": group.get("educationFormName"),
                "programs": [program["name"] for program in programs],
                "seats": capacities[group_id],
                "rawRating": rankings[group_id][application_id][0],
                "rawPlaceAmongConsents": ordered_applicants.index(application_id) + 1,
                "effectivePlace": effective_place,
                "assignedCount": len(application_ids),
                "vacancies": capacities[group_id] - len(application_ids),
                "cutoffRawRating": cutoff_rating,
                "isHighestPassingPriority": assigned_group_id == group_id,
                "updated": updates[group_id],
                "url": (
                    "https://www.gosuslugi.ru/vuznavigator/specialties/"
                    f"{group['oksoCode']}/{group['educationLevelId']}/"
                    f"{organization['organizationId']}/1/{program_ids}/-/applicants/{group_id}"
                    f"?program={group['educationLevelId']}_{group['educationFormId']}__"
                    f"{program_query}___"
                ),
            }
        )

    consent_values = sorted(
        {
            records[(group_id, application_id)].get("consent") or "NONE"
            for group_id in preferences[application_id]
        }
    )
    update_values = sorted(value for value in updates.values() if value)
    return {
        **organization,
        "applicationId": application_id,
        "consentValues": consent_values,
        "hypotheticalConsent": not any(value in ACTIVE_CONSENTS for value in consent_values),
        "highestPassingPriority": target_priorities.get(assigned_group_id),
        "assignedCompetitionId": assigned_group_id,
        "competitionGroups": len(groups),
        "simulatedApplicants": len(preferences),
        "assignedApplicants": len(assignments),
        "updateRange": {
            "oldest": update_values[0] if update_values else None,
            "newest": update_values[-1] if update_values else None,
        },
        "skippedStatuses": dict(sorted(skipped_statuses.items())),
        "results": target_rows,
    }


def simulate_priorities(args: argparse.Namespace) -> dict:
    organizations = resolve_organizations(args.university, args.org_id, args.city, args.year)
    return {
        "year": args.year,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "applicationId": args.application_id,
        "scope": "main budget places, main admission stage",
        "organizations": [
            simulate_priority_organization(organization, args.year, args.application_id)
            for organization in organizations
        ],
    }


def find_font(bold: bool = False) -> str:
    candidates = (
        [
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            r"C:\Windows\Fonts\arialbd.ttf",
        ]
        if bold
        else [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            r"C:\Windows\Fonts\arial.ttf",
        ]
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    raise RuntimeError("No Cyrillic TrueType font found (Arial or DejaVu Sans)")


def write_pdf(report: dict, output: Path) -> None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (
            PageBreak,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as error:
        raise RuntimeError("PDF generation requires: pip install reportlab") from error

    pdfmetrics.registerFont(TTFont("Admission", find_font()))
    pdfmetrics.registerFont(TTFont("AdmissionBold", find_font(bold=True)))
    styles = getSampleStyleSheet()

    body = ParagraphStyle(
        "Body", parent=styles["BodyText"], fontName="Admission", fontSize=9, leading=12, alignment=TA_LEFT
    )
    small = ParagraphStyle(
        "Small", parent=body, fontSize=8, leading=10, textColor=colors.HexColor("#536878")
    )
    tiny = ParagraphStyle(
        "Tiny", parent=body, fontSize=7, leading=9, textColor=colors.HexColor("#536878")
    )
    title = ParagraphStyle(
        "Title",
        parent=body,
        fontName="AdmissionBold",
        fontSize=22,
        leading=27,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#17324d"),
    )
    subtitle = ParagraphStyle(
        "Subtitle", parent=small, alignment=TA_CENTER, fontSize=10, leading=12
    )
    h1 = ParagraphStyle(
        "H1",
        parent=body,
        fontName="AdmissionBold",
        fontSize=16,
        leading=20,
        spaceBefore=10,
        spaceAfter=5,
        textColor=colors.HexColor("#17324d"),
    )
    h2 = ParagraphStyle(
        "H2",
        parent=body,
        fontName="AdmissionBold",
        fontSize=12,
        leading=15,
        spaceBefore=7,
        spaceAfter=3,
        textColor=colors.HexColor("#285a73"),
    )
    h3 = ParagraphStyle(
        "H3",
        parent=body,
        fontName="AdmissionBold",
        fontSize=10,
        leading=13,
        spaceBefore=5,
        spaceAfter=2,
        textColor=colors.HexColor("#17324d"),
    )

    VERDICT_FILL = {
        "passing": colors.HexColor("#eaf6ed"),
        "borderline": colors.HexColor("#fff9e6"),
        "not_passing": colors.HexColor("#f9eaea"),
    }
    VERDICT_STROKE = {
        "passing": colors.HexColor("#9bc7a5"),
        "borderline": colors.HexColor("#f0c36d"),
        "not_passing": colors.HexColor("#d4dadd"),
    }
    VERDICT_LABEL = {
        "passing": "✓ Проходите",
        "borderline": "~ На границе",
        "not_passing": "✗ Не проходите",
    }

    def paragraph(value: object, style=body):
        return Paragraph(html.escape(str(value)), style)

    def linked(label: str, url: str, style=body):
        return Paragraph(
            f'<link href="{html.escape(url, quote=True)}">{html.escape(label)}</link>',
            style,
        )

    def make_table(rows, col_widths, style_commands=None):
        table = Table(rows, colWidths=col_widths)
        base_style = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        if style_commands:
            base_style.extend(style_commands)
        table.setStyle(TableStyle(base_style))
        return table

    def result_card(item: dict) -> Table:
        place = (
            str(item["placeBest"])
            if item["placeBest"] == item["placeWorst"]
            else f"{item['placeBest']}–{item['placeWorst']}"
        )
        margin_text = (
            f"запас {item['margin']} мест"
            if item["margin"] >= 0
            else f"не хватает {-item['margin']} мест"
        )
        status = VERDICT_LABEL[item["verdict"]]
        fill = VERDICT_FILL[item["verdict"]]
        stroke = VERDICT_STROKE[item["verdict"]]

        # Exam rows
        exam_rows = [
            [
                paragraph("№", tiny),
                paragraph("Требования к экзаменам", tiny),
                paragraph("Твой балл", tiny),
            ]
        ]
        for idx, (exam_line, score) in enumerate(
            zip(item["exams"], item["candidateResults"]), start=1
        ):
            exam_rows.append(
                [
                    paragraph(str(idx), small),
                    paragraph(exam_line, small),
                    paragraph(str(score), small),
                ]
            )
        exam_table = make_table(
            exam_rows,
            [12 * mm, 104 * mm, 22 * mm],
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f6f7f8")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#d4dadd")),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (-1, 0), (-1, -1), "CENTER"),
            ],
        )

        programs = "; ".join(item["programs"]) or "без отдельного профиля"
        cost_text = (
            f"Стоимость: {item['costOfStudy']:,} ₽/год"
            if item.get("costOfStudy")
            else None
        )
        card_rows = [
            [paragraph(f"{item['code']} — {item['specialty']}", h3)],
            [paragraph(f"Профили / программы: {programs}", small)],
            [
                paragraph(
                    f"{status} · Место {place} из {item['seats']} · {margin_text} · "
                    f"Конкурсный балл {item['totalScore']} · Согласий {item['activeConsents']}",
                    body,
                )
            ],
        ]
        if cost_text:
            card_rows.append([paragraph(cost_text, small)])
        card_rows.extend([[exam_table], [linked(f"Обновлено: {item['updated'] or 'не указано'} — открыть список", item["url"], tiny)]])
        card = make_table([[rows] for rows in card_rows], [158 * mm])
        card.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), fill),
                    ("BOX", (0, 0), (-1, -1), 0.7, stroke),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        return card

    output.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
    )

    story: list[Any] = [
        Paragraph("Отчёт о шансах на поступление", title),
        Paragraph(f"Госуслуги · Вузнавигатор · {report['year']}", subtitle),
        Spacer(1, 6 * mm),
    ]

    profile = report["profile"]
    score_rows = [
        [paragraph("Предмет", h3), paragraph("Балл", h3)]
    ]
    for subject, score in profile["scores"].items():
        score_rows.append([paragraph(subject.capitalize(), body), paragraph(str(score), body)])
    score_rows.append(
        [
            paragraph("Индивидуальные достижения", body),
            paragraph(str(profile["individual"]), body),
        ]
    )
    score_table = make_table(
        score_rows,
        [70 * mm, 30 * mm],
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf5f7")),
            ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#c9dce2")),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c9dce2")),
            ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ],
    )

    info_rows = [
        [paragraph("Форма обучения", body), paragraph(report["form"], body)],
        [paragraph("Основа", body), paragraph(report["placeTypeName"], body)],
    ]
    if report.get("maxCost") is not None:
        info_rows.append(
            [paragraph("Макс. стоимость", body), paragraph(f"{report['maxCost']:,} ₽/год", body)]
        )
    info_table = make_table(
        info_rows,
        [45 * mm, 55 * mm],
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf5f7")),
            ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#c9dce2")),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c9dce2")),
        ],
    )

    story.append(
        make_table(
            [[score_table, info_table]],
            [105 * mm, 105 * mm],
            [("VALIGN", (0, 0), (-1, -1), "TOP")],
        )
    )
    story.append(Spacer(1, 3 * mm))
    story.append(
        paragraph(
            "Уровни определяются автоматически: бакалавриат и базовое высшее образование. "
            "Специалитет включается только по явному запросу. Данные текущие и не гарантируют зачисление.",
            small,
        )
    )
    story.append(Spacer(1, 4 * mm))

    # Summary statistics
    all_results = [item for university in report["universities"] for item in university["results"]]
    passing = [item for item in all_results if item["verdict"] == "passing"]
    borderline = [item for item in all_results if item["verdict"] == "borderline"]
    not_passing = [item for item in all_results if item["verdict"] == "not_passing"]

    story.append(Paragraph("Общая сводка", h1))
    stats_rows = [
        [paragraph("Показатель", h3), paragraph("Значение", h3)],
        [paragraph("Вузов в отчёте", body), paragraph(str(len(report["universities"])), body)],
        [paragraph("Конкурсных групп", body), paragraph(str(len(all_results)), body)],
        [paragraph("Проходите", body), paragraph(str(len(passing)), body)],
        [paragraph("На границе", body), paragraph(str(len(borderline)), body)],
        [paragraph("Не проходите", body), paragraph(str(len(not_passing)), body)],
    ]
    stats_table = make_table(
        stats_rows,
        [60 * mm, 35 * mm],
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf5f7")),
            ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#c9dce2")),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c9dce2")),
            ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ],
    )
    story.append(stats_table)
    story.append(Spacer(1, 4 * mm))

    # Passing overview table
    if passing:
        story.append(Paragraph("Направления, куда проходите", h2))
        overview_rows = [
            [
                paragraph("Вуз", tiny),
                paragraph("Направление", tiny),
                paragraph("Код", tiny),
                paragraph("Место", tiny),
                paragraph("Бюджет", tiny),
                paragraph("Запас", tiny),
            ]
        ]
        for item in sorted(passing, key=lambda x: -x["margin"]):
            university_name = next(
                (
                    university["name"]
                    for university in report["universities"]
                    if any(r["competitionId"] == item["competitionId"] for r in university["results"])
                ),
                "",
            )
            overview_rows.append(
                [
                    paragraph(university_name, tiny),
                    paragraph(item["specialty"], tiny),
                    paragraph(item["code"], tiny),
                    paragraph(str(item["placeWorst"]), tiny),
                    paragraph(str(item["seats"]), tiny),
                    paragraph(f"+{item['margin']}", tiny),
                ]
            )
        overview_table = make_table(
            overview_rows,
            [45 * mm, 42 * mm, 20 * mm, 15 * mm, 15 * mm, 15 * mm],
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf6ed")),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#9bc7a5")),
                ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d4dadd")),
                ("ALIGN", (3, 0), (-1, -1), "CENTER"),
            ],
        )
        story.append(overview_table)
        story.append(Spacer(1, 4 * mm))

    story.append(PageBreak())

    # Per-university detailed sections
    for index, university in enumerate(report["universities"]):
        if index:
            story.append(PageBreak())
        story.append(Paragraph(html.escape(university["name"]), h1))
        story.append(linked("Карточка вуза на Госуслугах", university["url"], small))
        story.append(
            paragraph(
                f"Уровни: {', '.join(LEVEL_NAMES.get(level, str(level)) for level in university['educationLevels'])}. "
                f"Форма: {university['form']}",
                small,
            )
        )
        story.append(Spacer(1, 3 * mm))

        if not university["results"]:
            story.append(paragraph("Подходящих конкурсных групп не найдено."))
            continue

        mini_summary = (
            f"Всего групп: {len(university['results'])}; "
            f"проходите: {sum(1 for r in university['results'] if r['verdict'] == 'passing')}; "
            f"на границе: {sum(1 for r in university['results'] if r['verdict'] == 'borderline')}; "
            f"не проходите: {sum(1 for r in university['results'] if r['verdict'] == 'not_passing')}"
        )
        story.append(paragraph(mini_summary, small))
        story.append(Spacer(1, 2 * mm))

        for item in university["results"]:
            story.append(result_card(item))
            story.append(Spacer(1, 2 * mm))

    document.build(story)


def build_report(args: argparse.Namespace) -> dict:
    scores = parse_scores(args.score)
    place_type_id = PLACE_TYPE_PAID if getattr(args, "paid", False) else PLACE_TYPE_BUDGET
    max_cost = getattr(args, "max_cost", None)
    organizations = resolve_organizations(args.university, args.org_id, args.city, args.year)
    universities = [
        analyze_organization(
            organization,
            args.year,
            scores,
            args.individual,
            args.total,
            args.education_level,
            args.include_specialist,
            args.form_id,
            place_type_id,
            max_cost,
        )
        for organization in organizations
    ]
    return {
        "year": args.year,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "form": FORM_NAMES[args.form_id],
        "placeTypeId": place_type_id,
        "placeTypeName": PLACE_TYPE_NAMES[place_type_id],
        "maxCost": max_cost,
        "profile": {"scores": scores, "individual": args.individual, "totalOverride": args.total},
        "universities": universities,
    }


def add_analysis_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--university", action="append", default=[])
    parser.add_argument("--org-id", type=int, action="append", default=[])
    parser.add_argument("--city")
    parser.add_argument("--year", type=int, default=datetime.now().year)
    parser.add_argument("--score", action="append", required=True, metavar="SUBJECT=SCORE")
    parser.add_argument("--individual", type=int, default=0)
    parser.add_argument("--total", type=int, help="Override the group-specific calculated total")
    parser.add_argument("--education-level", type=int, action="append")
    parser.add_argument("--include-specialist", action="store_true")
    parser.add_argument("--form-id", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--paid", action="store_true", help="Analyze paid places instead of budget")
    parser.add_argument("--max-cost", type=int, metavar="RUB", help="Maximum annual cost for paid places")


def validate_analysis_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not args.university and not args.org_id:
        parser.error("provide at least one --university or --org-id")
    if args.university and not args.city:
        parser.error("--city is required when searching by name")


def parse_args() -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = argparse.ArgumentParser(description="Current Russian university admission chances from Gosuslugi")
    subparsers = parser.add_subparsers(dest="command", required=True)

    find_parser = subparsers.add_parser("find", help="Find exact Gosuslugi organization IDs")
    find_parser.add_argument("--query", required=True)
    find_parser.add_argument("--city")
    find_parser.add_argument("--year", type=int, default=datetime.now().year)

    analyze_parser = subparsers.add_parser("analyze", help="Print analysis as JSON")
    add_analysis_arguments(analyze_parser)

    report_parser = subparsers.add_parser("report", help="Generate one JSON/PDF report")
    add_analysis_arguments(report_parser)
    report_parser.add_argument("--pdf", type=Path)
    report_parser.add_argument("--json", type=Path)

    track_parser = subparsers.add_parser("track", help="Track an application by idApplication in competitive lists")
    track_parser.add_argument("--application-id", type=int, required=True)
    track_parser.add_argument("--university", action="append", default=[])
    track_parser.add_argument("--org-id", type=int, action="append", default=[])
    track_parser.add_argument("--city")
    track_parser.add_argument("--year", type=int, default=datetime.now().year)
    track_parser.add_argument("--place-type-id", type=int, choices=(1, 3), default=PLACE_TYPE_BUDGET)

    priorities_parser = subparsers.add_parser(
        "priorities", help="Simulate stable main-budget assignment across all organization priorities"
    )
    priorities_parser.add_argument("--application-id", type=int, required=True)
    priorities_parser.add_argument("--university", action="append", default=[])
    priorities_parser.add_argument("--org-id", type=int, action="append", default=[])
    priorities_parser.add_argument("--city")
    priorities_parser.add_argument("--year", type=int, default=datetime.now().year)

    args = parser.parse_args()
    if args.command in {"analyze", "report"}:
        validate_analysis_args(parser, args)
    if args.command == "track" and not args.university and not args.org_id:
        parser.error("provide at least one --university or --org-id")
    if args.command == "track" and args.university and not args.city:
        parser.error("--city is required when searching by name")
    if args.command == "priorities" and not args.university and not args.org_id:
        parser.error("provide at least one --university or --org-id")
    if args.command == "priorities" and args.university and not args.city:
        parser.error("--city is required when searching by name")
    if args.command == "report" and not args.pdf and not args.json:
        parser.error("report requires --pdf and/or --json")
    return parser, args


def main() -> None:
    parser, args = parse_args()
    try:
        if args.command == "find":
            result = GosuslugiClient(0, args.year).find_organizations(args.query, args.city)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == "track":
            result = track_application(args)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == "priorities":
            result = simulate_priorities(args)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        report = build_report(args)
        if args.command == "analyze":
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(args.json)
        if args.pdf:
            write_pdf(report, args.pdf)
            print(args.pdf)
    except (GosuslugiError, RuntimeError, ValueError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    main()
