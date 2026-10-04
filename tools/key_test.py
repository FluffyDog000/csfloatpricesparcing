"""Проверка API-ключа CSFloat, в окне.

Вставь ключ, при желании прокси, нажми «Проверить». Скрипт отправит ТРИ
запроса — одно объявление из /api/v1/listings, стакан ордеров к нему и чтение
своего аккаунта (/api/v1/me, ничего не меняет) — и покажет лимиты CSFloat для
каждого: у них разные счётчики. Ключ и пароль прокси не сохраняются и
целиком не печатаются; ответ аккаунта не печатается вовсе.

Нужен только Python 3 — ничего ставить не надо:
    python csfloat_key_test.py

Где нет окон (сервер по SSH), скрипт спросит ключ прямо в консоли.
"""
import datetime
import hashlib
import json
import re
import time
import threading
import urllib.error
import urllib.request

URL = "https://csfloat.com/api/v1/listings?limit=1&type=buy_now"

MEANING = {
    200: "Ключ работает.",
    401: "Ключ не принят: неверный, отозван или вставлен с лишними символами.",
    403: "Доступ запрещён: ключ без прав или CSFloat не пускает этот адрес "
         "(VPN, датацентр). Попробуй без прокси или с другим.",
    429: "Лимит запросов: с этого адреса или ключа уже слишком много запросов. "
         "Подожди и повтори.",
}


BOOK = "https://csfloat.com/api/v1/listings/{id}/buy-orders?limit=1"
# Our own account: what placing, cancelling and reading orders are counted
# against. Read only - nothing on the account changes.
ME = "https://csfloat.com/api/v1/me"


def masked(proxy: str) -> str:
    """The proxy as printed: the password replaced, so the output can be
    pasted anywhere."""
    return re.sub(r"//([^:/@]+):([^@]+)@", r"//\1:***@", proxy)


def _get(opener, url: str, key: str, timeout: float):
    req = urllib.request.Request(url, headers={
        "Authorization": key,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (key test)",
    })
    try:
        resp = opener.open(req, timeout=timeout)
        return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _limits(headers) -> list[str]:
    quota = [(k, v) for k, v in headers.items()
             if k.lower().startswith(("x-ratelimit", "retry-after"))]
    if not quota:
        return ["  заголовков лимита нет"]
    out = [f"  {k}: {v}" for k, v in quota]
    reset = headers.get("x-ratelimit-reset")
    try:
        left = float(reset) - time.time()
        when = datetime.datetime.fromtimestamp(float(reset)).strftime("%H:%M:%S")
        out.append(f"  → счётчик обнулится в {when}, через "
                   + (f"{left / 60:.1f} мин" if left >= 60 else f"{left:.0f} с"))
    except (TypeError, ValueError):
        pass
    return out


def probe(key: str, proxy: str = "", url: str = URL, timeout: float = 20.0,
          book_url: str = BOOK, me_url: str = ME) -> str:
    """Три запроса: листинг, стакан ордеров к нему и свой аккаунт. У CSFloat
    у них разные лимиты. Возвращает отчёт текстом."""
    text = _market(key, proxy, url, timeout, book_url)
    return text + "\n\n" + _account(key, proxy, me_url, timeout)


def _account(key: str, proxy: str, me_url: str, timeout: float) -> str:
    """Чтение своего аккаунта. Тело ответа не печатается: там id и баланс,
    нужны только лимиты."""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    try:
        status, headers, body = _get(opener, me_url, key, timeout)
    except Exception as exc:
        return f"3. Свой аккаунт — не дошёл: {type(exc).__name__}: {exc}"
    lines = [f"3. Свой аккаунт (/me — как выставление и сверка) — HTTP {status}: "
             f"{MEANING.get(status, 'неожиданный ответ')}"]
    lines += _limits(headers)
    if status != 200:
        lines.append("  ответ: " + (body.decode("utf-8", "replace").strip()[:300]
                                    or "(пусто)"))
    return "\n".join(lines)


def _market(key: str, proxy: str, url: str, timeout: float,
            book_url: str) -> str:
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    try:
        status, headers, body = _get(opener, url, key, timeout)
    except Exception as exc:  # сеть, прокси, DNS, таймаут
        return f"Запрос не дошёл до CSFloat:\n{type(exc).__name__}: {exc}"

    lines = [f"Ключ: …{key[-4:]}  (отпечаток {hashlib.sha256(key.encode()).hexdigest()[:8]})",
             f"Через: {masked(proxy) if proxy else 'напрямую'}", "",
             f"1. Листинги — HTTP {status}: {MEANING.get(status, 'неожиданный ответ')}"]
    lines += _limits(headers)

    text = body.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    rows = data.get("data") if isinstance(data, dict) else data
    if not (status == 200 and isinstance(rows, list) and rows):
        lines.append("  ответ: " + (text.strip()[:500] or "(пусто)"))
        return "\n".join(lines)
    item = rows[0].get("item", {})
    lines.append(f"  пример: {item.get('market_hash_name', '?')}")

    listing_id = rows[0].get("id")
    if not listing_id:
        lines.append("\n2. Стакан ордеров — пропущен: в ответе нет id листинга")
        return "\n".join(lines)
    try:
        status, headers, body = _get(opener, book_url.format(id=listing_id),
                                     key, timeout)
    except Exception as exc:
        lines.append(f"\n2. Стакан ордеров — не дошёл: {type(exc).__name__}: {exc}")
        return "\n".join(lines)
    lines.append("")
    lines.append(f"2. Стакан ордеров (buy-orders) — HTTP {status}: "
                 f"{MEANING.get(status, 'неожиданный ответ')}")
    lines += _limits(headers)
    if status != 200:
        lines.append("  ответ: " + (body.decode('utf-8', 'replace').strip()[:300]
                                    or "(пусто)"))
    return "\n".join(lines)


def console() -> None:
    """Без окна: на сервере без графики tkinter обычно не установлен."""
    import getpass
    key = getpass.getpass("API-ключ (ввод не отображается): ").strip()
    proxy = input("Прокси (Enter — напрямую): ").strip()
    print()
    print(probe(key, proxy) if key else "Ключ пустой.")


def main() -> None:
    try:
        import tkinter as tk
        from tkinter import scrolledtext
        root = tk.Tk()
    except Exception:  # нет tkinter или нет экрана
        console()
        return
    root.title("Проверка ключа CSFloat")
    root.geometry("620x420")

    tk.Label(root, text="API-ключ:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
    key_entry = tk.Entry(root, show="•", width=60)
    key_entry.grid(row=0, column=1, sticky="we", padx=8)

    show = tk.BooleanVar(value=False)
    tk.Checkbutton(root, text="показать", variable=show,
                   command=lambda: key_entry.config(show="" if show.get() else "•")
                   ).grid(row=0, column=2, padx=4)

    tk.Label(root, text="Прокси (необяз.):").grid(row=1, column=0, sticky="w", padx=8)
    proxy_entry = tk.Entry(root, width=60)
    proxy_entry.grid(row=1, column=1, sticky="we", padx=8)
    tk.Label(root, text="http://user:pass@host:port", fg="gray").grid(
        row=2, column=1, sticky="w", padx=8)

    out = scrolledtext.ScrolledText(root, height=15, wrap="word")
    out.grid(row=4, column=0, columnspan=3, sticky="nsew", padx=8, pady=8)
    root.grid_columnconfigure(1, weight=1)
    root.grid_rowconfigure(4, weight=1)

    def show_result(text: str) -> None:
        out.delete("1.0", "end")
        out.insert("end", text)
        button.config(state="normal")

    def run() -> None:
        key = key_entry.get().strip()
        if not key:
            show_result("Вставь ключ.")
            return
        button.config(state="disabled")
        out.delete("1.0", "end")
        out.insert("end", "Отправляю три запроса…")
        proxy = proxy_entry.get().strip()
        # В отдельном потоке, чтобы окно не зависало, пока ждём ответ.
        threading.Thread(
            target=lambda: root.after(0, show_result, probe(key, proxy)),
            daemon=True).start()

    button = tk.Button(root, text="Проверить", command=run, width=16)
    button.grid(row=3, column=1, sticky="w", padx=8, pady=4)
    key_entry.bind("<Return>", lambda _e: run())
    key_entry.focus()
    root.mainloop()


if __name__ == "__main__":
    main()
