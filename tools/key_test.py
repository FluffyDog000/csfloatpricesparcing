"""Проверка API-ключа CSFloat: один запрос, в окне.

Вставь ключ, при желании прокси, нажми «Проверить». Скрипт отправит ОДИН
запрос (одно объявление из /api/v1/listings) и покажет, что ответил CSFloat.
Ключ никуда не сохраняется и не печатается целиком.

Нужен только Python 3 — ничего ставить не надо:
    python csfloat_key_test.py

Где нет окон (сервер по SSH), скрипт спросит ключ прямо в консоли.
"""
import datetime
import hashlib
import json
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


def probe(key: str, proxy: str = "", url: str = URL, timeout: float = 20.0) -> str:
    """Один запрос. Возвращает отчёт текстом."""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={
        "Authorization": key,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (key test)",
    })
    try:
        resp = opener.open(req, timeout=timeout)
        status, headers, body = resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        status, headers, body = exc.code, exc.headers, exc.read()
    except Exception as exc:  # сеть, прокси, DNS, таймаут
        return f"Запрос не дошёл до CSFloat:\n{type(exc).__name__}: {exc}"

    lines = [f"Ключ: …{key[-4:]}  (отпечаток {hashlib.sha256(key.encode()).hexdigest()[:8]})",
             f"Через: {proxy or 'напрямую'}",
             f"HTTP {status} — {MEANING.get(status, 'неожиданный ответ')}", ""]
    quota = [(k, v) for k, v in headers.items()
             if k.lower().startswith(("x-ratelimit", "retry-after"))]
    if quota:
        lines.append("Лимиты:")
        lines += [f"  {k}: {v}" for k, v in quota]
        reset = headers.get("x-ratelimit-reset")
        try:
            left = float(reset) - time.time()
            when = datetime.datetime.fromtimestamp(float(reset)).strftime("%H:%M:%S")
            lines.append(f"  → счётчик обнулится в {when}, через "
                         + (f"{left / 60:.1f} мин" if left >= 60 else f"{left:.0f} с"))
        except (TypeError, ValueError):
            pass
        lines.append("")

    text = body.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    rows = data.get("data") if isinstance(data, dict) else data
    if status == 200 and isinstance(rows, list) and rows:
        item = rows[0].get("item", {})
        price = rows[0].get("price")
        lines.append("Пример ответа:")
        lines.append(f"  {item.get('market_hash_name', '?')}")
        if price is not None:
            lines.append(f"  цена ${price / 100:.2f}, float {item.get('float_value')}")
    else:
        lines.append("Ответ сервера:")
        lines.append("  " + (text.strip()[:500] or "(пусто)"))
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
        out.insert("end", "Отправляю один запрос…")
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
