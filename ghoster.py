from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from PIL import Image, UnidentifiedImageError
except ImportError:  # icon resize is optional for CLI use
    Image = None
    UnidentifiedImageError = Exception


class GhostError(Exception):
    pass


SIGNATURE_SUFFIXES = (".SF", ".RSA", ".DSA", ".EC")
MAX_ENTRIES = 50_000
MAX_UNCOMPRESSED = 1_000_000_000
MAX_COVER_ASSET = 4 * 1024 * 1024
MAX_LANG_TOTAL = 16 * 1024 * 1024

FUNCTIONAL_KEYS = (
    "entrypoints",
    "mixins",
    "accessWidener",
    "depends",
    "recommends",
    "suggests",
    "breaks",
    "conflicts",
    "jars",
    "languageAdapters",
    "custom",
)


@dataclass
class GhostResult:
    path: Path
    filename: str
    meta: dict[str, Any]
    entries: int
    classes: int
    size: int
    warnings: list[str] = field(default_factory=list)


def _load_meta(zf: zipfile.ZipFile, label: str) -> dict[str, Any]:
    try:
        info = zf.getinfo("fabric.mod.json")
    except KeyError as exc:
        raise GhostError(f"{label} has no fabric.mod.json") from exc
    if info.file_size > MAX_COVER_ASSET:
        raise GhostError(f"{label} fabric.mod.json is too large ({info.file_size} bytes)")
    raw = zf.read(info)
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GhostError(f"{label} fabric.mod.json is invalid: {exc}") from exc
    if not isinstance(meta, dict) or not meta.get("id"):
        raise GhostError(f"{label} fabric.mod.json has no id")
    return meta


def _is_signature(name: str) -> bool:
    upper = name.upper()
    if not upper.startswith("META-INF/"):
        return False
    base = upper[len("META-INF/"):]
    return base.endswith(SIGNATURE_SUFFIXES) or base.startswith("SIG-")


def _manifest_main_section(raw: bytes) -> bytes:
    text = raw.decode("latin-1").replace("\r\n", "\n").replace("\r", "\n")
    head = text.split("\n\n", 1)[0].rstrip("\n")
    head = re.split(r"\nName:", head, maxsplit=1)[0].rstrip("\n")
    if not head.strip():
        return b"Manifest-Version: 1.0\r\nCreated-By: Fabric Loom\r\n\r\n"
    return (head.replace("\n", "\r\n") + "\r\n\r\n").encode("latin-1")


def _safe_filename(name: str | None, fallback: str) -> str:
    value = (name or fallback).strip()
    value = re.sub(r"[^A-Za-z0-9._+-]+", "_", value)
    if not value.lower().endswith(".jar"):
        value += ".jar"
    if len(value) > 180:
        value = value[:-4][:176] + ".jar"
    return value


def _zip_info(name: str, date_time: tuple[int, int, int, int, int, int],
              external_attr: int = 0o644 << 16) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=date_time)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = external_attr
    info.flag_bits |= 0x800
    return info


def _info_from_source(source_info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(source_info.filename, date_time=source_info.date_time)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = source_info.external_attr
    info.internal_attr = source_info.internal_attr
    info.create_system = source_info.create_system
    info.comment = source_info.comment
    info.flag_bits |= 0x800
    return info


def _resize_icon(cover_bytes: bytes, target_bytes: bytes, target_name: str) -> bytes:
    if Image is None:
        raise RuntimeError("Pillow is not installed")
    try:
        cover_image = Image.open(io.BytesIO(cover_bytes))
    except UnidentifiedImageError as exc:
        raise RuntimeError("cover icon is not a readable image") from exc
    try:
        target_image = Image.open(io.BytesIO(target_bytes))
        size = target_image.size
    except UnidentifiedImageError:
        size = (256, 256)
    resized = cover_image.convert("RGBA").resize(size, Image.Resampling.NEAREST)
    buffer = io.BytesIO()
    if target_name.lower().endswith((".jpg", ".jpeg")):
        resized.convert("RGB").save(buffer, "JPEG", quality=92)
    else:
        resized.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def ghost_jar(
    source: str | Path,
    cover: str | Path,
    out_dir: str | Path,
    *,
    id_mode: str = "keep",
    custom_id: str | None = None,
    version: str | None = None,
    filename: str | None = None,
    replace_icon: bool = True,
    copy_lang: bool = True,
) -> GhostResult:
    source_path = Path(source)
    cover_path = Path(cover)
    output_dir = Path(out_dir)

    if not source_path.is_file():
        raise GhostError(f"source jar not found: {source_path}")
    if not cover_path.is_file():
        raise GhostError(f"cover jar not found: {cover_path}")
    if not zipfile.is_zipfile(source_path):
        raise GhostError(f"source is not a valid jar/zip: {source_path}")
    if not zipfile.is_zipfile(cover_path):
        raise GhostError(f"cover is not a valid jar/zip: {cover_path}")
    output_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []

    with zipfile.ZipFile(source_path) as source_zip, zipfile.ZipFile(cover_path) as cover_zip:
        source_infos = source_zip.infolist()
        cover_infos = cover_zip.infolist()

        if len(source_infos) > MAX_ENTRIES:
            raise GhostError(f"source jar has too many entries ({len(source_infos)})")
        if len(cover_infos) > MAX_ENTRIES:
            raise GhostError(f"cover jar has too many entries ({len(cover_infos)})")
        if len({info.filename for info in source_infos}) != len(source_infos):
            raise GhostError("source jar contains duplicate entry names")
        if len({info.filename for info in cover_infos}) != len(cover_infos):
            raise GhostError("cover jar contains duplicate entry names")

        source_uncompressed = sum(info.file_size for info in source_infos)
        cover_uncompressed = sum(info.file_size for info in cover_infos)
        if source_uncompressed > MAX_UNCOMPRESSED:
            raise GhostError(f"source jar expands to {source_uncompressed} bytes, over the limit")
        if cover_uncompressed > MAX_UNCOMPRESSED:
            raise GhostError(f"cover jar expands to {cover_uncompressed} bytes, over the limit")

        try:
            bad = source_zip.testzip()
            if bad:
                raise GhostError(f"source jar is corrupt at {bad}")
            bad = cover_zip.testzip()
            if bad:
                raise GhostError(f"cover jar is corrupt at {bad}")
        except zipfile.BadZipFile as exc:
            raise GhostError(f"corrupt jar: {exc}") from exc

        source_meta = _load_meta(source_zip, "source")
        cover_meta = _load_meta(cover_zip, "cover")

        source_id = str(source_meta["id"])
        cover_id = str(cover_meta["id"])

        if id_mode == "keep":
            output_id = source_id
        elif id_mode == "cover":
            output_id = cover_id
        elif id_mode == "custom":
            if not custom_id:
                raise GhostError("id_mode=custom requires a custom_id")
            output_id = custom_id
        else:
            raise GhostError(f"unknown id_mode: {id_mode}")

        if not re.fullmatch(r"[a-z0-9_-]{1,64}", output_id):
            raise GhostError(f"invalid mod id: {output_id!r}")

        output_version = str(version or cover_meta.get("version") or source_meta.get("version") or "1.0.0")

        cover_icon_path = cover_meta.get("icon")
        cover_icon_bytes: bytes | None = None
        cover_icon_info: zipfile.ZipInfo | None = None
        if isinstance(cover_icon_path, str):
            try:
                cover_icon_info = cover_zip.getinfo(cover_icon_path)
                if cover_icon_info.file_size <= MAX_COVER_ASSET:
                    cover_icon_bytes = cover_zip.read(cover_icon_path)
                else:
                    warnings.append("cover icon is unusually large; skipped")
            except KeyError:
                warnings.append(f"cover icon missing from archive: {cover_icon_path}")

        icon_parts = (cover_icon_path or "").split("/")
        cover_namespace = icon_parts[1] if len(icon_parts) >= 3 and icon_parts[0] == "assets" else cover_id
        lang_prefix = f"assets/{cover_namespace}/lang/"

        cover_lang: dict[str, bytes] = {}
        cover_lang_times: dict[str, tuple[int, int, int, int, int, int]] = {}
        if copy_lang:
            lang_total = 0
            lang_truncated = False
            for info in cover_infos:
                if info.filename.startswith(lang_prefix) and info.filename.endswith(".json"):
                    if info.file_size > MAX_COVER_ASSET or lang_total + info.file_size > MAX_LANG_TOTAL:
                        lang_truncated = True
                        continue
                    data = cover_zip.read(info.filename)
                    lang_total += len(data)
                    cover_lang[info.filename] = data
                    cover_lang_times[info.filename] = info.date_time
            if lang_truncated:
                warnings.append("cover language files exceeded the copy cap; some were skipped")

        output_meta: dict[str, Any] = {
            "schemaVersion": 1,
            "id": output_id,
            "version": output_version,
            "name": cover_meta.get("name") or source_meta.get("name") or output_id,
            "description": cover_meta.get("description", ""),
        }
        for key in ("authors", "contributors", "contact", "license"):
            if key in cover_meta:
                output_meta[key] = cover_meta[key]
        if isinstance(cover_icon_path, str) and cover_icon_bytes:
            output_meta["icon"] = cover_icon_path
        elif isinstance(source_meta.get("icon"), str):
            output_meta["icon"] = source_meta["icon"]
        output_meta["environment"] = source_meta.get("environment", cover_meta.get("environment", "client"))
        for key in FUNCTIONAL_KEYS:
            if key in source_meta:
                output_meta[key] = source_meta[key]

        if id_mode == "keep" and source_id != cover_id:
            warnings.append(
                f"internal mod ID kept as '{source_id}' so the client's own lookups still resolve; "
                f"visible name, version, and icon match the cover"
            )
        elif id_mode == "cover":
            warnings.append(
                f"mod ID set to '{cover_id}'; remove the real cover jar from mods or Fabric will reject the duplicate"
            )
        elif id_mode == "custom":
            warnings.append(f"mod ID set to '{output_id}'")

        source_icon_path = source_meta.get("icon")
        replacements: dict[str, bytes] = {}
        if replace_icon and cover_icon_bytes and isinstance(source_icon_path, str):
            if source_icon_path in source_zip.namelist() and source_icon_path != cover_icon_path:
                source_icon_info = source_zip.getinfo(source_icon_path)
                if source_icon_info.file_size > MAX_COVER_ASSET:
                    warnings.append(f"source icon is too large to replace: {source_icon_path}")
                else:
                    try:
                        replacements[source_icon_path] = _resize_icon(
                            cover_icon_bytes, source_zip.read(source_icon_path), source_icon_path
                        )
                    except Exception as exc:
                        warnings.append(f"source icon not replaced: {exc}")
            elif source_icon_path not in source_zip.namelist():
                warnings.append(f"source icon path missing from archive: {source_icon_path}")

        skip = {"fabric.mod.json", "META-INF/MANIFEST.MF"}
        skip.update(cover_lang)
        if isinstance(cover_icon_path, str):
            skip.add(cover_icon_path)

        signature_count = sum(1 for info in source_infos if _is_signature(info.filename))
        if signature_count:
            warnings.append(f"stripped {signature_count} jar signature entr{'y' if signature_count == 1 else 'ies'}; output is unsigned")

        manifest_info = next((info for info in source_infos if info.filename == "META-INF/MANIFEST.MF"), None)
        meta_info = next((info for info in source_infos if info.filename == "fabric.mod.json"), None)
        default_time = manifest_info.date_time if manifest_info else (2024, 1, 1, 0, 0, 0)
        meta_time = meta_info.date_time if meta_info else default_time

        try:
            manifest_bytes = _manifest_main_section(source_zip.read("META-INF/MANIFEST.MF"))
        except KeyError:
            manifest_bytes = b"Manifest-Version: 1.0\r\nCreated-By: Fabric Loom\r\n\r\n"

        meta_bytes = (json.dumps(output_meta, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

        default_name = f"{cover_id}-{output_version}.jar"
        output_name = _safe_filename(filename, default_name)
        output_path = output_dir / output_name
        if output_path.resolve() in (source_path.resolve(), cover_path.resolve()):
            raise GhostError("output path would overwrite an input jar")
        if output_path.exists():
            output_path.unlink()

        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as output_zip:
            output_zip.writestr(_zip_info("META-INF/MANIFEST.MF", default_time), manifest_bytes)
            output_zip.writestr(_zip_info("fabric.mod.json", meta_time), meta_bytes)

            if cover_icon_bytes and isinstance(cover_icon_path, str):
                icon_time = cover_icon_info.date_time if cover_icon_info else default_time
                output_zip.writestr(_zip_info(cover_icon_path, icon_time), cover_icon_bytes)

            for name, data in cover_lang.items():
                output_zip.writestr(_zip_info(name, cover_lang_times.get(name, default_time)), data)

            for info in source_infos:
                name = info.filename
                if name in skip or _is_signature(name):
                    continue
                if name in replacements:
                    output_zip.writestr(_info_from_source(info), replacements[name])
                    continue
                if name.endswith("/"):
                    output_zip.writestr(_info_from_source(info), b"")
                    continue
                destination = _info_from_source(info)
                with source_zip.open(info) as reader, output_zip.open(destination, "w") as writer:
                    shutil.copyfileobj(reader, writer, 1024 * 1024)

    with zipfile.ZipFile(output_path) as check_zip:
        bad = check_zip.testzip()
        if bad:
            raise GhostError(f"output jar failed verification at {bad}")
        entries = len(check_zip.infolist())
        classes = sum(1 for info in check_zip.infolist() if info.filename.endswith(".class"))

    return GhostResult(
        path=output_path,
        filename=output_name,
        meta=output_meta,
        entries=entries,
        classes=classes,
        size=output_path.stat().st_size,
        warnings=warnings,
    )


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebrand a Fabric mod jar as a cover mod.")
    parser.add_argument("source", help="jar to hide")
    parser.add_argument("cover", help="jar to impersonate")
    parser.add_argument("-o", "--out", default="out", help="output directory")
    parser.add_argument("--id-mode", choices=("keep", "cover", "custom"), default="keep")
    parser.add_argument("--custom-id", default=None)
    parser.add_argument("--version", default=None)
    parser.add_argument("--filename", default=None)
    parser.add_argument("--no-icon", action="store_true", help="do not replace the source's own icon")
    parser.add_argument("--no-lang", action="store_true", help="do not copy the cover's language files")
    args = parser.parse_args(argv)

    try:
        result = ghost_jar(
            args.source,
            args.cover,
            args.out,
            id_mode=args.id_mode,
            custom_id=args.custom_id,
            version=args.version,
            filename=args.filename,
            replace_icon=not args.no_icon,
            copy_lang=not args.no_lang,
        )
    except GhostError as exc:
        print(f"ghost failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({
        "path": str(result.path),
        "filename": result.filename,
        "id": result.meta["id"],
        "name": result.meta.get("name"),
        "version": result.meta.get("version"),
        "entries": result.entries,
        "classes": result.classes,
        "size": result.size,
        "warnings": result.warnings,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
