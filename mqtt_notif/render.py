"""Moteur de rendu des gabarits : substitution de champs, jamais d'exception.

Moteur maison minimal (aucune bibliothèque de template, aucun filtre ni
logique scriptée) : '{{' -> '{' littéral, '{chemin}' -> valeur résolue par
segments dans le dict du payload (chemins pointés), champ ou chemin absent
-> '[absent]'. La syntaxe est validée au chargement de la config ; le rendu
lui-même est total : aucune erreur de rendu ne peut atteindre la boucle.
Limite connue : une clé contenant un point n'est pas adressable ('{a.b}' est
une syntaxe de chemin, jamais une clé littérale).
"""

from __future__ import annotations

import json

from .rules import PAYLOAD_FORMAT_RAW, Rule

MISSING = "[absent]"
RAW_PLACEHOLDER = "payload"


class TemplateError(Exception):
    """Gabarit invalide : erreur de configuration (échec rapide au démarrage)."""


def _tokenize(template: str) -> list[tuple[str, str]]:
    """Découpe un gabarit en segments ('text', contenu) et ('field', chemin).

    '{{' produit un segment texte contenant un '{' littéral, sans tentative
    de substitution ; un '{' simple ouvre un placeholder jusqu'au '}' le plus
    proche. Lève TemplateError sur gabarit malformé (accolade non fermée,
    placeholder vide, espace ou '{' dans le placeholder).
    """
    tokens: list[tuple[str, str]] = []
    text: list[str] = []
    index = 0
    while index < len(template):
        char = template[index]
        if char == "{" and template.startswith("{{", index):
            text.append("{")
            index += 2
            continue
        if char != "{":
            text.append(char)
            index += 1
            continue
        end = template.find("}", index + 1)
        if end < 0:
            raise TemplateError("accolade non fermée")
        field = template[index + 1 : end]
        if not field:
            raise TemplateError("placeholder vide")
        if any(char.isspace() for char in field):
            raise TemplateError("placeholder avec espace interdit")
        if "{" in field:
            raise TemplateError("'{' interdit dans un placeholder")
        if any(not segment for segment in field.split(".")):
            raise TemplateError("segment de chemin vide")
        if text:
            tokens.append(("text", "".join(text)))
            text = []
        tokens.append(("field", field))
        index = end + 1
    if text:
        tokens.append(("text", "".join(text)))
    return tokens


def validate_template(template: str, payload_format: str, rule_name: str, field_name: str) -> None:
    """Valide la syntaxe d'un gabarit ; l'erreur cite la règle et le champ fautifs.

    Mode raw : seul le placeholder '{payload}' est autorisé, tout autre
    placeholder est rejeté au chargement.
    """
    try:
        tokens = _tokenize(template)
    except TemplateError as exc:
        raise TemplateError(
            f"Règle '{rule_name}' : champ '{field_name}' : gabarit '{template}' "
            f"invalide ({exc})"
        ) from exc
    if payload_format == PAYLOAD_FORMAT_RAW:
        for _kind, field in tokens:
            if _kind == "field" and field != RAW_PLACEHOLDER:
                raise TemplateError(
                    f"Règle '{rule_name}' : champ '{field_name}' : gabarit '{template}' "
                    f"invalide (placeholder '{{{field}}}' interdit en mode raw, "
                    f"seul '{{{RAW_PLACEHOLDER}}}' est autorisé)"
                )


def _format_value(value: object) -> str:
    """Rend une valeur substituée : str() pour les scalaires, None -> 'null',
    booléens -> 'true'/'false', dict/list du chemin final en JSON compact."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _resolve_field(field: str, payload: dict[str, object] | str) -> str:
    """Résout un placeholder : chemin pointé dans le dict (mode json),
    chaîne brute pour '{payload}' (mode raw) ; absent -> '[absent]'."""
    if not isinstance(payload, dict):
        return payload if field == RAW_PLACEHOLDER else MISSING
    current: object = payload
    for segment in field.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            return MISSING
    return _format_value(current)


def render_template(template: str, payload: dict[str, object] | str) -> str:
    """Substitue les placeholders d'un gabarit validé ; jamais d'exception.

    Champ ou chemin absent -> '[absent]' ; '{{' -> '{' littéral.
    """
    rendered: list[str] = []
    for kind, content in _tokenize(template):
        rendered.append(_resolve_field(content, payload) if kind == "field" else content)
    return "".join(rendered)


def render_message(rule: Rule, payload: dict[str, object] | str) -> dict[str, str]:
    """Rend le message complet d'une règle depuis son gabarit et le payload figé.

    Retourne toujours 'title' et 'short_message' ; 'detailed_message' seulement
    si la règle le définit (la clé n'est pas envoyée dans le message FCM sinon).
    """
    message = {
        "title": render_template(rule.title, payload),
        "short_message": render_template(rule.short_message, payload),
    }
    if rule.detailed_message is not None:
        message["detailed_message"] = render_template(rule.detailed_message, payload)
    return message
