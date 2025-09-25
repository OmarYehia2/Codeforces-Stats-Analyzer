import threading
import csv
import sys
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime, timezone, timedelta
from collections import Counter
from statistics import mean
import requests # type: ignore

API_BASE = "https://codeforces.com/api"

def normalize_handle(raw):
    h = (raw or "").strip()
    if not h:
        return ""
    if h.startswith("<") and h.endswith(">"):
        h = h[1:-1].strip()
    return h

def parse_date_maybe(s):
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        if len(s) == 10 and s.count("-") == 2:
            dt = datetime.strptime(s, "%Y-%m-%d")
            return dt.replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        raise ValueError("Date must be YYYY-MM-DD or ISO datetime")

def friendly_md(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%m-%d")

def api_user_status(handle, _from, count, timeout=20):
    url = f"{API_BASE}/user.status"
    params = {"handle": handle, "from": _from, "count": count}
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    if j.get("status") != "OK":
        raise RuntimeError(f"Codeforces API error: {j.get('comment', j)}")
    return j.get("result", [])

# ---------------- Data logic ----------------
def fetch_submissions_in_period(handle, dt_from, dt_to, page_size=1000, max_requests=100, progress=None):
    if dt_from and dt_to and dt_from > dt_to:
        raise ValueError("From date must be <= To date")

    from_ts = int(dt_from.timestamp()) if dt_from else None
    to_ts = int(dt_to.timestamp()) if dt_to else None

    start_idx = 1
    requests_made = 0
    yielded = 0

    while True:
        if requests_made >= max_requests:
            if progress:
                progress(f"Reached max_requests={max_requests}; stopping.")
            break
        requests_made += 1
        if progress:
            progress(f"API request #{requests_made}: from={start_idx} count={page_size}")
        page = api_user_status(handle, _from=start_idx, count=page_size)
        if not page:
            if progress:
                progress("No more submissions returned by API.")
            break

        oldest_ts_in_page = None
        for s in page:
            ts = int(s.get("creationTimeSeconds", 0))
            if oldest_ts_in_page is None or ts < oldest_ts_in_page:
                oldest_ts_in_page = ts
            if from_ts is not None and ts < from_ts:
                continue
            if to_ts is not None and ts > to_ts:
                continue
            yield s
            yielded += 1

        if oldest_ts_in_page is not None and from_ts is not None and oldest_ts_in_page < from_ts:
            if progress:
                progress("Oldest submission in page is older than From date -> stopping early.")
            break

        start_idx += len(page)

    if progress:
        progress(f"Finished fetching. Yielded {yielded} submissions in period.")

def collect_first_ac_per_problem(submissions_iter):
    solved = {}
    for s in submissions_iter:
        if s.get("verdict") != "OK":
            continue
        p = s.get("problem", {})
        ts = int(s.get("creationTimeSeconds", 0))
        if p.get("contestId") is not None and p.get("index"):
            key = f"{p['contestId']}-{p['index']}"
        else:
            key = f"nopid-{p.get('name','unknown')}"
        if key not in solved or ts < solved[key][0]:
            solved[key] = (ts, p.get("tags", []), p.get("name", ""), p.get("rating"))
    return solved

# ---------------- GUI ----------------
class CF:
    def __init__(self, root):
        self.root = root
        root.title("Codeforces Tags by Period")
        root.geometry("840x560")
        root.minsize(700, 480)
        big_font = ("Consolas", 14)

        style = ttk.Style()
        style.configure("TLabel", font=("Arial", 12))
        style.configure("TButton", font=("Arial", 12))
        style.configure("TEntry", font=("Arial", 12))

        main = ttk.Frame(root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(main)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Handle:").pack(side=tk.LEFT)
        self.handle_var = tk.StringVar()
        self.handle_entry = ttk.Entry(top, textvariable=self.handle_var, width=28)
        self.handle_entry.pack(side=tk.LEFT, padx=(6,12))
        self.handle_entry.bind("<Return>", lambda e: self.start_fetch())

        ttk.Label(top, text="From (YYYY-MM-DD):").pack(side=tk.LEFT)
        self.from_var = tk.StringVar()
        self.from_entry = ttk.Entry(top, textvariable=self.from_var, width=12)
        self.from_entry.pack(side=tk.LEFT, padx=(6,12))

        ttk.Label(top, text="To (YYYY-MM-DD):").pack(side=tk.LEFT)
        self.to_var = tk.StringVar()
        self.to_entry = ttk.Entry(top, textvariable=self.to_var, width=12)
        self.to_entry.pack(side=tk.LEFT, padx=(6,12))

        self.fetch_btn = ttk.Button(top, text="Fetch", command=self.start_fetch)
        self.fetch_btn.pack(side=tk.LEFT, padx=(6,0))

        self.save_btn = ttk.Button(top, text="Save CSV", command=self.save_csv, state=tk.DISABLED)
        self.save_btn.pack(side=tk.LEFT, padx=(8,0))

        self.clear_btn = ttk.Button(top, text="Clear", command=self.clear_output)
        self.clear_btn.pack(side=tk.LEFT, padx=(8,0))

        self.last30_btn = ttk.Button(top, text="Set Last 30d", command=self.set_last_30)
        self.last30_btn.pack(side=tk.LEFT, padx=(12,0))

        self.status = tk.StringVar(value="Ready.")
        ttk.Label(main, textvariable=self.status).pack(fill=tk.X, pady=(8,8))

        body = ttk.Frame(main)
        body.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(body)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        right = ttk.Frame(body, width=300)
        right.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Label(left, text="Tags").pack(anchor="w")
        self.tags_text = tk.Text(left, height=25, wrap=tk.NONE, font=big_font)
        self.tags_text.pack(fill=tk.BOTH, expand=True, padx=(0,8), pady=(6,8))

        ttk.Label(right, text="Solved problems (detail)").pack(anchor="w")
        self.solved_text = tk.Text(right, width=100, wrap=tk.NONE, font=big_font)
        self.solved_text.pack(fill=tk.BOTH, expand=True, pady=(6,0))
        
        self.stats_var = tk.StringVar(value="No data.")
        ttk.Label(right, textvariable=self.stats_var, justify=tk.LEFT).pack(anchor="w", pady=(6,4))
        
        self.solved_map = {}

        if requests is None:
            self.fetch_btn.config(state=tk.DISABLED)
            self.status.set("Missing 'requests' module. Install with: python -m pip install requests")
        
        self.set_last_30()

    def set_status(self, txt):
        self.root.after(0, lambda: self.status.set(txt))

    def set_last_30(self):
        today = datetime.now(timezone.utc).date()
        last30 = today - timedelta(days=30)
        self.from_var.set(last30.strftime("%Y-%m-%d"))
        self.to_var.set(today.strftime("%Y-%m-%d"))

    def start_fetch(self):
        if requests is None:
            messagebox.showerror("Missing dependency", "Python module 'requests' is not installed.\nRun:\n\npython -m pip install requests")
            return
        raw_handle = self.handle_var.get()
        handle = normalize_handle(raw_handle)
        if not handle:
            messagebox.showwarning("No handle", "Please enter a Codeforces handle.")
            return

        try:
            dt_from = parse_date_maybe(self.from_var.get())
            dt_to = parse_date_maybe(self.to_var.get())
        except Exception as e:
            messagebox.showerror("Date parse error", str(e))
            return

        if dt_to is None:
            dt_to = datetime.now(timezone.utc)
        if dt_from is None:
            dt_from = datetime.now(timezone.utc) - timedelta(days=30)

        if dt_to.hour == 0 and dt_to.minute == 0 and dt_to.second == 0 and len(self.to_var.get().strip()) == 10:
            dt_to = dt_to.replace(hour=23, minute=59, second=59)

        self.fetch_btn.config(state=tk.DISABLED)
        self.save_btn.config(state=tk.DISABLED)
        self.clear_output()
        self.set_status(f"Fetching for {handle} from {dt_from.date()} to {dt_to.date()} ...")

        thread = threading.Thread(target=self._fetch_thread, args=(handle, dt_from, dt_to), daemon=True)
        thread.start()

    def _fetch_thread(self, handle, dt_from, dt_to):
        try:
            def progress(msg):
                self.set_status(msg)
            subs_gen = fetch_submissions_in_period(handle, dt_from, dt_to, page_size=1000, max_requests=200, progress=progress)
            solved_map = collect_first_ac_per_problem(subs_gen)

            tag_counts = {} 
            tag_ratings = {} 
            all_ratings = [] 

            for (ts, tags, nm, rating) in solved_map.values():
                rating_val = None
                if rating is not None:
                    try:
                        rating_val = float(rating)
                        all_ratings.append(rating_val)
                    except Exception:
                        rating_val = None

                for tag in tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
                    if rating_val is not None:
                        lst = tag_ratings.setdefault(tag, [])
                        lst.append(rating_val)

            try:
                user_info = api_user_info(handle) # type: ignore
                user_rating = user_info.get("rating")
                user_max_rating = user_info.get("maxRating")
            except Exception:
                user_info = None
                user_rating = None
                user_max_rating = None
        
            tag_lines = []
            if tag_counts:
                for tag, cnt in sorted(tag_counts.items(), key=lambda x: (-x[1], x[0])):
                    rlist = tag_ratings.get(tag, [])
                    if rlist:
                        min_r = int(min(rlist))
                        max_r = int(max(rlist))
                        avg_r = round(sum(rlist) / len(rlist), 1)
                        tag_lines.append(f"{tag}: Counter = {cnt} | Min = {min_r} | Max = {max_r} | Avg = {avg_r}")
                    else:
                        tag_lines.append(f"{tag}: Counter = {cnt} | Min = N/A | Max = N/A | Avg = N/A")
            else:
                tag_lines = ["(none)"]

            problems_count = len(solved_map)
            period_seconds = max(1.0, (dt_to - dt_from).total_seconds())
            period_days = period_seconds / 86400.0
            solve_rate = problems_count / period_days if period_days > 0 else float('inf')
            avg_problem_rating = (round(sum(all_ratings) / len(all_ratings), 1)
                                  if all_ratings else None)

            prob_lines = []
            solved_detail_lines = []
            for key, (ts, tags, name, rating) in sorted(solved_map.items(), key=lambda x: -x[1][0]):
                md = friendly_md(ts)
                prob_lines.append(f"{key} | {md} | {name} | tags: {','.join(tags)}")
                solved_detail_lines.append(f"{key}\t{md}\t{rating if rating is not None else ''}\t{','.join(tags)}")

            stats_parts = []
            stats_parts.append(f"Period: {dt_from.date()} → {dt_to.date()} ({period_days:.2f} days)")
            stats_parts.append(f"Problems (unique OK in period): {problems_count}")
            stats_parts.append(f"Solve rate: {solve_rate:.3f} problems/day")

            if avg_problem_rating is not None:
                stats_parts.append(f"Average problem rating: {avg_problem_rating:.1f}")
            else:
                stats_parts.append("Average problem rating: N/A (no rating fields)")

            if user_rating is not None:
                stats_parts.append(f"Current rating: {user_rating}")
            if user_max_rating is not None:
                stats_parts.append(f"Max rating: {user_max_rating}")

            stats_text = "\n".join(stats_parts)
            def finish():
                try:
                    self.tags_text.delete("1.0", tk.END)
                    self.tags_text.insert(tk.END, "\n".join(tag_lines))

                    if hasattr(self, "solved_text"):
                        self.solved_text.delete("1.0", tk.END)
                        self.solved_text.insert(tk.END, "\n".join(solved_detail_lines))

                    if all_ratings:
                        avg_rating = round(sum(all_ratings) / len(all_ratings), 2)
                        min_rating = int(min(all_ratings))
                        max_rating = int(max(all_ratings))
                        stats_display = f"Problems solved have Rating: {len(all_ratings)} | Avg rating: {avg_rating} | Min: {min_rating} | Max: {max_rating}"
                    else:
                        stats_display = "No problems with ratings in this period."

                    self.stats_var.set(stats_display)
                    self.solved_map = solved_map
                    self.set_status(f"Done. Unique OK problems in period: {len(solved_map)}")
                    self.fetch_btn.config(state=tk.NORMAL)
                    if solved_map:
                        self.save_btn.config(state=tk.NORMAL)
                except Exception as ui_err:
                    import traceback
                    traceback.print_exc()
                    messagebox.showerror("UI update error", f"Exception updating UI:\n{ui_err}")
            

            self.root.after(0, finish)
        except Exception as e:
            self.root.after(0, lambda: self._on_error(e))

    def _on_error(self, e):
        self.fetch_btn.config(state=tk.NORMAL)
        self.save_btn.config(state=tk.DISABLED)
        self.set_status("Error: " + str(e))
        messagebox.showerror("Error", f"Failed to fetch data:\n{e}")

    def save_csv(self):
        if not self.solved_map:
            messagebox.showinfo("Nothing to save", "No solved problems to save.")
            return
        
        fname = filedialog.asksaveasfilename(defaultextension=".csv",
                                                filetypes=[("CSV files","*.csv"),("All files","*.*")],
                                                title="Save solved problems CSV")
        
        if not fname:
            return
        try:
            with open(fname, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["key","first_ac_mmdd","name","rating","tags"])
                for key, (ts, tags, name, rating) in sorted(self.solved_map.items(), key=lambda x: -x[1][0]):
                    md = friendly_md(ts)
                    writer.writerow([key, md, name, rating if rating is not None else "", ";".join(tags)])
            messagebox.showinfo("Saved", f"CSV saved to:\n{fname}")
        except Exception as e:
            messagebox.showerror("Save error", f"Failed to save CSV:\n{e}")


    def clear_output(self):
        self.tags_text.delete("1.0", tk.END)

        if hasattr(self, "solved_text"):
            self.solved_text.delete("1.0", tk.END)

        self.solved_map = {}
        self.save_btn.config(state=tk.DISABLED)
        self.set_status("Ready.")


def main():
    root = tk.Tk()
    app = CF(root)
    root.mainloop()

if __name__ == "__main__":
    main()
