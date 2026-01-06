import json
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import os, sys

# Ensure local imports work when running from repo root
sys.path.insert(0, os.path.dirname(__file__))
from scheduler_deadline import DeadlineScheduler

class MissionGUI:
    def __init__(self, root):
        self.root = root
        self.root.title('AUFalkon - Control-Layer Deadline Simulator')

        self.mission = None
        self.alive = {}
        self.scheduler = None
        self.tick_label = tk.StringVar(value='Tick: 0')
        self.recover_at = {}
        self.permanent_down = set()

        top = ttk.Frame(root); top.pack(fill='x', padx=10, pady=10)
        ttk.Button(top, text='Load Mission JSON', command=self.load_mission).pack(side='left')
        ttk.Button(top, text='Start', command=self.start).pack(side='left', padx=5)
        ttk.Button(top, text='Step', command=self.step).pack(side='left', padx=5)
        ttk.Button(top, text='Reset', command=self.reset).pack(side='left', padx=5)
        ttk.Label(top, textvariable=self.tick_label).pack(side='right')

        self.units_frame = ttk.LabelFrame(root, text='Units (toggle alive/down)')
        self.units_frame.pack(fill='x', padx=10, pady=5)
        self.unit_vars = {}

        self.domains_frame = ttk.LabelFrame(root, text='Domains (assignments last tick)')
        self.domains_frame.pack(fill='x', padx=10, pady=5)
        self.domain_labels = {}

        actions = ttk.Frame(root); actions.pack(fill='x', padx=10, pady=5)
        ttk.Label(actions, text='Selected unit:').pack(side='left')
        self.sel_unit = tk.StringVar(value='')
        self.unit_combo = ttk.Combobox(actions, textvariable=self.sel_unit, values=[])
        self.unit_combo.pack(side='left', padx=5)

        ttk.Label(actions, text='Temp fail (ms):').pack(side='left', padx=(10,2))
        self.temp_ms = tk.IntVar(value=10000)
        ttk.Entry(actions, textvariable=self.temp_ms, width=8).pack(side='left')

        ttk.Button(actions, text='Temporary Fail', command=self.temp_fail).pack(side='left', padx=5)
        ttk.Button(actions, text='Permanent Fail', command=self.perm_fail).pack(side='left', padx=5)

    def load_mission(self):
        path = filedialog.askopenfilename(filetypes=[('JSON','*.json')])
        if not path:
            return
        with open(path, 'r', encoding='utf-8') as f:
            self.mission = json.load(f)

        # Init alive map
        self.alive = {u: True for u in self.mission['units']}

        # Build unit toggles
        for w in self.units_frame.winfo_children():
            w.destroy()

        cols = 6
        self.unit_vars = {}
        for i, u in enumerate(self.mission['units']):
            var = tk.BooleanVar(value=True)
            self.unit_vars[u] = var
            cb = ttk.Checkbutton(self.units_frame, text=u, variable=var)
            cb.grid(row=i//cols, column=i%cols, padx=4, pady=2, sticky='w')

        # Update selector
        self.unit_combo['values'] = self.mission['units']
        self.sel_unit.set(self.mission['units'][0])

        # Domain labels
        for w in self.domains_frame.winfo_children():
            w.destroy()
        self.domain_labels = {}
        for d in self.mission['domains']:
            lbl = ttk.Label(self.domains_frame, text=f"{d}: -")
            lbl.pack(anchor='w')
            self.domain_labels[d] = lbl

        messagebox.showinfo('Mission', 'Mission loaded. Click Start, then Step.')

    def init_scheduler(self):
        tick_ms = float(self.mission.get('tick_ms', 1.0))
        max_gap_ms = int(self.mission['constraints']['max_gap_ms'])
        max_gap_ticks = max(1, int(max_gap_ms / tick_ms))
        required = int(self.mission['required_active_per_domain'])

        pools = {d: self.mission['domain_pools'].get(d, []) for d in self.mission['domains']}
        pools['spares'] = self.mission['domain_pools'].get('spares', [])

        self.scheduler = DeadlineScheduler(self.mission['domains'], pools, required, max_gap_ticks, tick_ms,
                                           capacity_per_unit=2,
                                           logs_dir='gui_logs')
        self.recover_at = {}
        self.permanent_down = set()

    def start(self):
        if not self.mission:
            messagebox.showerror('Error', 'Load a mission first.')
            return
        self.init_scheduler()
        self.tick_label.set('Tick: 0')

    def step(self):
        if not self.scheduler:
            messagebox.showerror('Error', 'Click Start first.')
            return

        current_ms = int(self.scheduler.tick * self.scheduler.tick_ms)
        # Apply scheduled recoveries for temporary failures
        for u, ts in list(self.recover_at.items()):
            if ts is not None and current_ms >= ts and u not in self.permanent_down:
                self.unit_vars[u].set(True)
                self.recover_at[u] = None

        # Build alive map
        for u, var in self.unit_vars.items():
            self.alive[u] = bool(var.get())

        try:
            assignments = self.scheduler.schedule_tick(self.alive)
        except Exception as e:
            messagebox.showerror('Invariant Failure', str(e))
            return

        by_domain = {d: [] for d in self.mission['domains']}
        for d, u in assignments:
            by_domain[d].append(u)
        for d in self.mission['domains']:
            self.domain_labels[d].configure(text=f"{d}: {', '.join(by_domain[d]) if by_domain[d] else '-'}")

        self.tick_label.set(f'Tick: {self.scheduler.tick}')

    def reset(self):
        self.scheduler = None
        self.tick_label.set('Tick: 0')
        for d in self.domain_labels:
            self.domain_labels[d].configure(text=f"{d}: -")
        self.recover_at = {}
        self.permanent_down = set()

    def temp_fail(self):
        if not self.scheduler:
            return
        u = self.sel_unit.get()
        if not u:
            return
        self.unit_vars[u].set(False)
        duration = int(self.temp_ms.get())
        current_ms = int(self.scheduler.tick * self.scheduler.tick_ms)
        self.recover_at[u] = current_ms + duration

    def perm_fail(self):
        if not self.scheduler:
            return
        u = self.sel_unit.get()
        if not u:
            return
        self.unit_vars[u].set(False)
        self.permanent_down.add(u)


if __name__ == '__main__':
    root = tk.Tk()
    gui = MissionGUI(root)
    root.mainloop()
