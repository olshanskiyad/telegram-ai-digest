import re

PROMO_TAGS = (
    '#реклама',
    '#промо',
)

GENERAL_PROMO_PATTERNS = (
    'доступ открыт',
    'открыт доступ',
    'открыта регистрация',
    'регистрация открыта',
    'открыт набор',
    'набор открыт',
    'места ограничены',
    'последний шанс',
    'только до',
    'скидка',
    'промокод',
    'early bird',
    'discount',
    'sale',
    'вход открыт',
    'открываю доступ',
    'закрою доступ',
    'лимит по местам',
    'платной группы',
    'ежемесячной подпиской',
    'startup battlefield',
)

TG_EVENT_PROMO_PATTERNS = (
    'практикум',
    'вебинар',
    'интенсив',
    'мастеркласс',
    'мастер-класс',
    'мастер класс',
    'воркшоп',
    'мастермайнд',
)

PROMO_CTA_PATTERNS = (
    'записаться',
    'запись на',
    'приглашаю на',
    'приглашаем на',
    'присоединяйтесь',
    'присоединиться',
    'купить',
    'оплатить',
    'билеты',
    'билет',
    'стоимость',
    'цена',
    'старт потока',
    'стартуем',
    'осталось мест',
)


def _normalize(text: str) -> str:
    lowered = (text or '').casefold()
    return re.sub(r'\s+', ' ', lowered).strip()


def looks_promotional_text(text: str) -> bool:
    lowered = _normalize(text)
    if not lowered:
        return False
    if any(tag in lowered for tag in PROMO_TAGS):
        return True
    if any(pattern in lowered for pattern in GENERAL_PROMO_PATTERNS):
        return True
    cta_hits = sum(1 for pattern in PROMO_CTA_PATTERNS if pattern in lowered)
    if cta_hits >= 2:
        return True
    if cta_hits >= 1 and re.search(r'\b(курс|созвон|менторинг|разбор|event|conference|summit)\b', lowered):
        return True
    if re.search(r'остал(?:о|ось|ись)\s+\d+\s+мест', lowered):
        return True
    if re.search(r'\b\d+%\s*скидк', lowered):
        return True
    return False


def is_excluded_telegram_post(text: str) -> bool:
    lowered = _normalize(text)
    return looks_promotional_text(lowered) or any(pattern in lowered for pattern in TG_EVENT_PROMO_PATTERNS)
