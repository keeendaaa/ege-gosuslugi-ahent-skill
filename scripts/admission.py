#!/usr/bin/env python3
import argparse
import html
import json
import os
import re
import sys
import time
from collections import defaultdict
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


def available_levels(catalog: list[dict], form_id: int) -> list[int]:
    return sorted(
        {
            item["educationLevelId"]
            for item in catalog
            if item.get("placeTypeId") == 1
            and item.get("stageAdmissionId") == 1
            and item.get("educationFormId") == form_id
            and item.get("numberPlaces", 0) > 0
        }
    )


def resolve_levels(
    catalog: list[dict], requested: list[int] | None, include_specialist: bool, form_id: int
) -> list[int]:
    if requested:
        return list(dict.fromkeys(requested))
    available = available_levels(catalog, form_id)
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
) -> list[dict]:
    candidates = {
        item["id"]: item
        for item in catalog
        if item.get("educationLevelId") in levels
        and item.get("placeTypeId") == 1
        and item.get("stageAdmissionId") == 1
        and item.get("educationFormId") == form_id
        and item.get("numberPlaces", 0) > 0
    }
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
    seats = group["numberPlaces"]
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
) -> dict:
    client = GosuslugiClient(organization["organizationId"], year)
    catalog = client.catalog()
    levels = resolve_levels(catalog, requested_levels, include_specialist, form_id)
    groups = relevant_groups(client, catalog, levels, form_id, scores)
    results = [
        analyze_group(client, group, scores, individual, total_override) for group in groups
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
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (
            KeepTogether,
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
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontName="Admission", fontSize=9, leading=12)
    small = ParagraphStyle("Small", parent=body, fontSize=8, leading=10, textColor=colors.HexColor("#536878"))
    title = ParagraphStyle("Title", parent=body, fontName="AdmissionBold", fontSize=22, leading=27, alignment=TA_CENTER, textColor=colors.HexColor("#17324d"))
    h1 = ParagraphStyle("H1", parent=body, fontName="AdmissionBold", fontSize=16, leading=20, spaceBefore=10, spaceAfter=5, textColor=colors.HexColor("#17324d"))
    h2 = ParagraphStyle("H2", parent=body, fontName="AdmissionBold", fontSize=11, leading=14, spaceBefore=7, spaceAfter=3, textColor=colors.HexColor("#285a73"))

    def paragraph(value: object, style=body):
        return Paragraph(html.escape(str(value)), style)

    def linked(label: str, url: str, style=body):
        return Paragraph(f'<link href="{html.escape(url, quote=True)}">{html.escape(label)}</link>', style)

    output.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(output), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=15 * mm, bottomMargin=15 * mm
    )
    story: list[Any] = [
        Paragraph("Шансы на поступление", title),
        Paragraph(f"Текущие конкурсные списки Госуслуг, {report['year']}", ParagraphStyle("Subtitle", parent=small, alignment=TA_CENTER, fontSize=10)),
        Spacer(1, 7 * mm),
    ]
    profile = report["profile"]
    profile_rows = [
        [paragraph(f"Баллы ЕГЭ: {', '.join(f'{key}={value}' for key, value in profile['scores'].items())}"), paragraph(f"Индивидуальные достижения: {profile['individual']}")],
        [paragraph(f"Форма: {report['form']}"), paragraph("Конкурс: основные бюджетные места")],
    ]
    profile_table = Table(profile_rows, colWidths=[84 * mm, 74 * mm])
    profile_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#edf5f7")), ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#c9dce2")), ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c9dce2")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story.extend([profile_table, Spacer(1, 4 * mm), paragraph("Уровни определяются автоматически: бакалавриат и базовое высшее образование. Специалитет включается только по явному запросу. Позиции текущие и не гарантируют зачисление.", small), Paragraph("Краткий итог", h1)])

    for university in report["universities"]:
        results = university["results"]
        passing = [item for item in results if item["verdict"] == "passing"]
        summary = f"внутри бюджета в {len(passing)} из {len(results)} подходящих групп"
        story.append(paragraph(f"• {university['name']}: {summary}"))

    for index, university in enumerate(report["universities"]):
        if index:
            story.append(PageBreak())
        story.extend([
            Paragraph(html.escape(university["name"]), h1),
            linked("Карточка вуза на Госуслугах", university["url"], small),
            paragraph("Уровни: " + ", ".join(LEVEL_NAMES.get(level, str(level)) for level in university["educationLevels"]), small),
            Spacer(1, 2 * mm),
        ])
        if not university["results"]:
            story.append(paragraph("Подходящих конкурсных групп не найдено."))
            continue
        for item in university["results"]:
            place = str(item["placeBest"]) if item["placeBest"] == item["placeWorst"] else f"{item['placeBest']}–{item['placeWorst']}"
            status = "сейчас внутри бюджета" if item["verdict"] == "passing" else "на границе" if item["verdict"] == "borderline" else "сейчас вне бюджета"
            margin = f"запас {item['margin']}" if item["margin"] >= 0 else f"не хватает {-item['margin']}"
            fill = colors.HexColor("#eaf6ed") if item["verdict"] == "passing" else colors.HexColor("#f6f7f8")
            content = [
                Paragraph(html.escape(f"{item['code']} — {item['specialty']}"), h2),
                paragraph(f"{status}; место {place} из {item['seats']}; {margin} мест; конкурсный балл {item['totalScore']}"),
                paragraph(f"Согласий: {item['activeConsents']}. Профиль: {'; '.join(item['programs']) or 'без отдельного профиля'}"),
                linked(f"Обновлено: {item['updated'] or 'не указано'} — открыть список", item["url"], small),
            ]
            table = Table([[content]], colWidths=[158 * mm])
            table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), fill), ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#9bc7a5") if item["verdict"] == "passing" else colors.HexColor("#d4dadd")), ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
            story.extend([KeepTogether(table), Spacer(1, 2 * mm)])

    document.build(story)


def build_report(args: argparse.Namespace) -> dict:
    scores = parse_scores(args.score)
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
        )
        for organization in organizations
    ]
    return {
        "year": args.year,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "form": FORM_NAMES[args.form_id],
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

    args = parser.parse_args()
    if args.command in {"analyze", "report"}:
        validate_analysis_args(parser, args)
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
