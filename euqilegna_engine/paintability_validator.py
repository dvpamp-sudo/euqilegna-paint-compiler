from __future__ import annotations

from collections import Counter
from pathlib import Path
import json
import xml.etree.ElementTree as ET

from .models import QualityReport, RegionGeometry, ValidationIssue


SVG_NS = "{http://www.w3.org/2000/svg}"


def validate_package(
    output_dir: Path,
    regions: list[RegionGeometry],
    palette: list[dict],
) -> QualityReport:
    issues: list[ValidationIssue] = []
    region_ids = [region.region_id for region in regions]

    duplicates = [
        region_id
        for region_id, count in Counter(region_ids).items()
        if count > 1
    ]
    for region_id in duplicates:
        issues.append(
            ValidationIssue(
                code="duplicate_region_id",
                message="Region ID occurs more than once.",
                region_id=region_id,
                repairable=False,
            )
        )

    palette_ids = {str(item.get("colorId")) for item in palette}
    for region in regions:
        if region.color_id not in palette_ids:
            issues.append(
                ValidationIssue(
                    code="missing_palette_color",
                    message=f"Color {region.color_id} is missing from the palette.",
                    region_id=region.region_id,
                    repairable=False,
                )
            )
        if not region.is_vectorized:
            issues.append(
                ValidationIssue(
                    code="empty_path",
                    message="Region has no SVG geometry.",
                    region_id=region.region_id,
                )
            )
        if not region.is_labelable:
            issues.append(
                ValidationIssue(
                    code="invalid_label",
                    message="Region has no usable interior number position.",
                    region_id=region.region_id,
                )
            )

    svg_path = output_dir / "paintMap.svg"
    if not svg_path.is_file():
        issues.append(
            ValidationIssue(
                code="missing_svg",
                message="paintMap.svg was not generated.",
                repairable=False,
            )
        )
        return QualityReport(
            passed=False,
            region_count=len(regions),
            simulated_count=0,
            numbered_count=0,
            clickable_count=0,
            hintable_count=0,
            palette_count=len(palette),
            issues=issues,
        )

    root = ET.fromstring(svg_path.read_text(encoding="utf-8"))
    paint_paths = {
        element.get("id"): element
        for element in root.findall(f".//{SVG_NS}path")
        if element.get("class") == "paint-region"
    }
    labels = {
        element.get("data-region-id"): element
        for element in root.findall(f".//{SVG_NS}text")
        if element.get("class") == "region-number"
    }
    hit_targets = {
        element.get("data-target-region"): element
        for element in root.findall(f".//{SVG_NS}path")
        if element.get("class") == "region-hit"
    }

    simulated = 0
    hintable = 0
    for region in regions:
        path = paint_paths.get(region.region_id)
        label = labels.get(region.region_id)
        hit = hit_targets.get(region.region_id)

        if path is None:
            issues.append(
                ValidationIssue(
                    code="missing_svg_path",
                    message="Region is missing from the SVG.",
                    region_id=region.region_id,
                )
            )
        if label is None:
            issues.append(
                ValidationIssue(
                    code="missing_number",
                    message="Region has no visible number label.",
                    region_id=region.region_id,
                )
            )
        elif (label.text or "").strip() != region.color_id:
            issues.append(
                ValidationIssue(
                    code="number_mismatch",
                    message="Displayed number does not match the palette color.",
                    region_id=region.region_id,
                )
            )
        else:
            hintable += 1

        if hit is None:
            issues.append(
                ValidationIssue(
                    code="missing_hit_target",
                    message="Region has no enlarged click target.",
                    region_id=region.region_id,
                )
            )

        if path is not None and label is not None and hit is not None:
            simulated += 1

    passed = not any(issue.severity == "error" for issue in issues)
    return QualityReport(
        passed=passed,
        region_count=len(regions),
        simulated_count=simulated,
        numbered_count=len(labels),
        clickable_count=len(hit_targets),
        hintable_count=hintable,
        palette_count=len(palette),
        issues=issues,
        metrics={
            "svgPaintPaths": len(paint_paths),
            "uniqueRegionIds": len(set(region_ids)),
        },
    )


def write_quality_report(output_dir: Path, report: QualityReport) -> None:
    (output_dir / "platform_quality_report.json").write_text(
        json.dumps(report.to_dict(), indent=2),
        encoding="utf-8",
    )
