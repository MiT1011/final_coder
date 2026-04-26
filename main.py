"""Interview Assistant v2 - Single Tkinter App."""
import tkinter as tk
from tkinter import scrolledtext, simpledialog
import threading, sys, ctypes
from datetime import datetime
from PIL import ImageGrab, ImageTk
import keyboard
import config as cfg
from groq_service import GroqService, SessionManager, img_to_b64, get_api_key, save_api_key

def _win_hwnd(title):
    if sys.platform=="win32":
        return ctypes.windll.user32.FindWindowW(None,title)
    return 0

def apply_stealth(hwnd):
    if sys.platform=="win32" and hwnd:
        try: ctypes.windll.user32.SetWindowDisplayAffinity(hwnd,0x00000011)
        except: pass

def set_click_through(hwnd,enable):
    if sys.platform!="win32" or not hwnd: return
    GWL=-20; LAY=0x00080000; TRN=0x00000020
    s=ctypes.windll.user32.GetWindowLongW(hwnd,GWL)
    if enable: ctypes.windll.user32.SetWindowLongW(hwnd,GWL,s|LAY|TRN)
    else: ctypes.windll.user32.SetWindowLongW(hwnd,GWL,s&~TRN)

def _ask_api_key():
    """Show a dialog to collect Groq API key on first run."""
    tmp = tk.Tk()
    tmp.withdraw()
    key = simpledialog.askstring(
        "Interview Assistant — API Key",
        "Enter your Groq API key:\n(Get one free at console.groq.com)",
        parent=tmp
    )
    tmp.destroy()
    if key and key.strip():
        save_api_key(key.strip())
        return key.strip()
    return None

class CopyableChat(scrolledtext.ScrolledText):
    def __init__(self,master,**kw):
        kw.setdefault("cursor","")
        super().__init__(master,**kw)
        self.bind("<Key>",self._block)
        self.bind("<Control-c>",self._copy)
        self.bind("<Control-C>",self._copy)
        self.bind("<Control-a>",self._sel_all)
        self.bind("<Control-A>",self._sel_all)
    def _block(self,e):
        if e.keysym in {"Left","Right","Up","Down","Home","End","Prior","Next"}: return None
        if e.state&0x4 and e.keysym.lower() in ("c","a","x"): return None
        return "break"
    def _copy(self,e=None):
        try:
            t=self.get(tk.SEL_FIRST,tk.SEL_LAST)
            self.clipboard_clear(); self.clipboard_append(t)
        except tk.TclError: pass
        return "break"
    def _sel_all(self,e=None):
        self.tag_add(tk.SEL,"1.0",tk.END); return "break"

class InterviewAssistant:
    WIN_TITLE="IA_Stealth_Overlay"
    def __init__(self):
        # --- Get API key (prompt if missing) ---
        api_key = get_api_key()
        if not api_key:
            api_key = _ask_api_key()
            if not api_key:
                sys.exit(0)  # User cancelled
        self.root=tk.Tk()
        self.root.title(self.WIN_TITLE)
        self.root.geometry(f"{cfg.WINDOW_WIDTH}x{cfg.WINDOW_HEIGHT}+{cfg.WINDOW_X}+{cfg.WINDOW_Y}")
        self.root.configure(bg=cfg.BG)
        self.root.attributes("-topmost",True)
        self.root.attributes("-alpha",cfg.DEFAULT_OPACITY)
        self.root.overrideredirect(True)
        self.groq=GroqService(api_key=api_key)
        self.session=SessionManager()
        self.current_page="chat"
        self.screenshots=[]; self.ss_photos=[]
        self._drag_x=0; self._drag_y=0
        self.is_thinking=False; self.click_through=False
        self.settings_open=False; self._alpha=cfg.DEFAULT_OPACITY
        self._hidden=False; self._hwnd=0
        self._build_titlebar(); self._build_body()
        self.root.update_idletasks()
        self.root.after(200,self._init_stealth)
        self._start_session(); self._register_hotkeys()
        self.show_chat_page(); self.root.mainloop()

    def _init_stealth(self):
        self._hwnd=_win_hwnd(self.WIN_TITLE); apply_stealth(self._hwnd)

    def _build_titlebar(self):
        tb=tk.Frame(self.root,bg="#010409",height=34)
        tb.pack(fill="x"); tb.pack_propagate(False)
        tb.bind("<ButtonPress-1>",self._drag_start)
        tb.bind("<B1-Motion>",self._drag_motion)
        tk.Label(tb,text="⚡",bg="#010409",fg=cfg.ACCENT,font=("Segoe UI",12)).pack(side="left",padx=(8,2))
        tk.Label(tb,text="Interview Assistant",bg="#010409",fg=cfg.FG,font=("Segoe UI",9,"bold")).pack(side="left")
        self.status_lbl=tk.Label(tb,text="● connecting",bg="#010409",fg=cfg.YELLOW,font=cfg.FONT_TINY)
        self.status_lbl.pack(side="left",padx=6)
        self._tb_btn(tb,"✕",self._quit,cfg.RED,side="right")
        self._tb_btn(tb,"⚙",self._toggle_settings,cfg.FG2,side="right")

    def _tb_btn(self,p,text,cmd,color,side="right"):
        l=tk.Label(p,text=text,bg="#010409",fg=color,font=("Segoe UI",11,"bold"),cursor="",padx=6,pady=2)
        l.pack(side=side); l.bind("<Button-1>",lambda e:cmd())
        l.bind("<Enter>",lambda e:l.config(fg=cfg.FG)); l.bind("<Leave>",lambda e:l.config(fg=color))
        return l

    def _arrow_btn(self,p,cmd):
        b=tk.Label(p,text="  ➤  ",bg=cfg.ACCENT,fg=cfg.BG,font=("Segoe UI",13,"bold"),cursor="",padx=4,pady=4,relief="flat")
        b.bind("<Button-1>",lambda e:cmd())
        b.bind("<Enter>",lambda e:b.config(bg=cfg.FG,fg=cfg.BG))
        b.bind("<Leave>",lambda e:b.config(bg=cfg.ACCENT,fg=cfg.BG))
        return b

    def _pill(self,p,text,cmd,bg_c,fg_c=None):
        fg_c=fg_c or cfg.BG
        b=tk.Label(p,text=text,bg=bg_c,fg=fg_c,font=("Segoe UI",9,"bold"),padx=8,pady=4,cursor="",relief="flat")
        b.bind("<Button-1>",lambda e:cmd())
        b.bind("<Enter>",lambda e:b.config(fg=bg_c,bg=fg_c))
        b.bind("<Leave>",lambda e:b.config(fg=fg_c,bg=bg_c))
        return b

    def _toggle_settings(self):
        if self.settings_open: self._settings_frame.destroy(); self.settings_open=False
        else: self._build_settings_panel(); self.settings_open=True

    def _build_settings_panel(self):
        pnl=tk.Frame(self.root,bg=cfg.BG2,highlightbackground=cfg.ACCENT,highlightthickness=1)
        pnl.place(relx=1.0,rely=0.0,anchor="ne",x=-2,y=36); self._settings_frame=pnl
        hdr=tk.Frame(pnl,bg=cfg.BG3); hdr.pack(fill="x")
        tk.Label(hdr,text="⚙  Settings & Shortcuts",bg=cfg.BG3,fg=cfg.ACCENT,font=("Segoe UI",9,"bold"),padx=10,pady=6).pack(side="left")
        cx=tk.Label(hdr,text="✕",bg=cfg.BG3,fg=cfg.FG2,cursor="",font=("Segoe UI",11),padx=8)
        cx.pack(side="right"); cx.bind("<Button-1>",lambda e:self._toggle_settings())
        tk.Frame(pnl,bg=cfg.BG3,height=1).pack(fill="x")
        orow=tk.Frame(pnl,bg=cfg.BG2); orow.pack(fill="x",padx=10,pady=8)
        tk.Label(orow,text="Opacity:",bg=cfg.BG2,fg=cfg.FG2,font=cfg.FONT_SML,width=10,anchor="w").pack(side="left")
        vl=tk.Label(orow,text=f"{int(self._alpha*100)}%",bg=cfg.BG2,fg=cfg.FG,font=cfg.FONT_SML,width=4); vl.pack(side="right")
        def on_s(v):
            a=round(float(v),2); self._alpha=a; self.root.attributes("-alpha",a); vl.config(text=f"{int(a*100)}%")
        sl=tk.Scale(orow,from_=0.1,to=1.0,resolution=0.05,orient="horizontal",command=on_s,bg=cfg.BG2,fg=cfg.FG,troughcolor=cfg.BG3,highlightthickness=0,showvalue=False,sliderlength=14,length=150)
        sl.set(self._alpha); sl.pack(side="left",padx=4)
        tk.Frame(pnl,bg=cfg.BG3,height=1).pack(fill="x",pady=(4,0))
        tk.Label(pnl,text="Keyboard Shortcuts",bg=cfg.BG2,fg=cfg.ACCENT,font=("Segoe UI",8,"bold")).pack(anchor="w",padx=10,pady=(6,2))
        for key,desc in [("Alt+Shift+S","Screenshot mode"),("Alt+G","Capture screenshot"),("Alt+B","Back to Chat"),("Alt+E","Submit / Send"),("Alt+T","Toggle click-through"),("Alt+H","Hide / Un-hide"),("Alt+Q","Quit"),("Alt+S","Settings"),("Alt+↑↓←→","Move window")]:
            r=tk.Frame(pnl,bg=cfg.BG2); r.pack(fill="x",padx=10,pady=1)
            tk.Label(r,text=key,bg=cfg.BG3,fg=cfg.PURPLE,font=("Consolas",8),padx=4,pady=1).pack(side="left")
            tk.Label(r,text=f"  {desc}",bg=cfg.BG2,fg=cfg.FG2,font=cfg.FONT_TINY).pack(side="left")
        tk.Frame(pnl,bg=cfg.BG2,height=8).pack()

    def _build_body(self):
        self.tab_frame=tk.Frame(self.root,bg=cfg.BG2); self.tab_frame.pack(fill="x")
        self.tab_chat=self._make_tab("💬 Chat","chat")
        self.tab_ss=self._make_tab("📸 Screenshot","screenshot")
        self.pages=tk.Frame(self.root,bg=cfg.BG); self.pages.pack(fill="both",expand=True)
        self._build_chat_page(); self._build_screenshot_page()

    def _make_tab(self,label,page):
        t=tk.Label(self.tab_frame,text=label,bg=cfg.BG2,fg=cfg.FG2,font=cfg.FONT_UI,padx=14,pady=5,cursor="")
        t.pack(side="left"); t.bind("<Button-1>",lambda e,p=page:self._switch_page(p))
        return t

    def _activate_tab(self,tab):
        for t in [self.tab_chat,self.tab_ss]: t.config(bg=cfg.BG2,fg=cfg.FG2)
        tab.config(bg=cfg.BG,fg=cfg.ACCENT)

    def _build_chat_page(self):
        self.chat_page=tk.Frame(self.pages,bg=cfg.BG)
        self.chat_display=CopyableChat(self.chat_page,wrap="word",bg=cfg.BG,fg=cfg.FG,font=cfg.FONT_MONO,relief="flat",padx=10,pady=6,state="disabled")
        self.chat_display.pack(fill="both",expand=True,padx=4,pady=4)
        self._config_tags(self.chat_display)
        inp=tk.Frame(self.chat_page,bg=cfg.BG2); inp.pack(fill="x",padx=4,pady=(0,2))
        self.chat_entry=tk.Text(inp,height=3,bg=cfg.BG3,fg=cfg.FG,insertbackground=cfg.ACCENT,font=cfg.FONT_MONO,relief="flat",padx=6,pady=4,wrap="word")
        self.chat_entry.pack(fill="x",padx=(4,0),pady=4,side="left",expand=True)
        self.chat_entry.bind("<Return>",self._on_enter)
        self.chat_entry.bind("<Shift-Return>",lambda e:None)
        self._arrow_btn(inp,self._send_chat).pack(side="right",padx=4,pady=4,fill="y")
        hr=tk.Frame(self.chat_page,bg=cfg.BG); hr.pack(fill="x",padx=4,pady=(0,4))
        tk.Label(hr,text="Enter / Alt+E → Send  │  Shift+Enter → Newline",bg=cfg.BG,fg=cfg.FG2,font=cfg.FONT_TINY).pack(side="left",padx=6)
        self._pill(hr,"Clear 🗑",self._clear_chat_display,cfg.BG3,cfg.FG2).pack(side="right",padx=4)

    def _build_screenshot_page(self):
        self.ss_page=tk.Frame(self.pages,bg=cfg.BG)
        tk.Label(self.ss_page,text="Alt+G → Capture  │  Max 3 screenshots  │  Alt+B → Back",bg=cfg.BG2,fg=cfg.FG2,font=cfg.FONT_TINY).pack(fill="x",padx=4,pady=2)
        self.thumb_frame=tk.Frame(self.ss_page,bg=cfg.BG2,height=90)
        self.thumb_frame.pack(fill="x",padx=4,pady=4); self.thumb_frame.pack_propagate(False)
        self._show_empty_thumb()
        pr=tk.Frame(self.ss_page,bg=cfg.BG2); pr.pack(fill="x",padx=4,pady=(0,4))
        tk.Label(pr,text="Prompt:",bg=cfg.BG2,fg=cfg.FG2,font=cfg.FONT_SML).pack(side="left",padx=6)
        self.ss_prompt=tk.Entry(pr,bg=cfg.BG3,fg=cfg.FG,insertbackground=cfg.ACCENT,font=cfg.FONT_MONO,relief="flat")
        self.ss_prompt.pack(fill="x",expand=True,padx=(4,0),side="left")
        self.ss_prompt.insert(0,"Analyze and answer any questions in the screenshots")
        self.ss_prompt.bind("<Return>",lambda e:self._get_ss_answer())
        self._arrow_btn(pr,self._get_ss_answer).pack(side="right",padx=4,pady=4)
        br=tk.Frame(self.ss_page,bg=cfg.BG); br.pack(fill="x",padx=4,pady=(0,4))
        self._pill(br,"📸 Capture (Alt+G)",self._capture_screenshot,cfg.GREEN).pack(side="left",padx=4)
        self._pill(br,"← Back (Alt+B)",lambda:self._switch_page("chat"),cfg.BG3,cfg.FG2).pack(side="right",padx=4)
        self.ss_count_var=tk.StringVar(value="0 / 3")
        tk.Label(br,textvariable=self.ss_count_var,bg=cfg.BG,fg=cfg.FG2,font=cfg.FONT_SML).pack(side="left",padx=8)
        self.ss_display=CopyableChat(self.ss_page,wrap="word",bg=cfg.BG,fg=cfg.FG,font=cfg.FONT_MONO,relief="flat",padx=10,pady=6,state="disabled")
        self.ss_display.pack(fill="both",expand=True,padx=4,pady=4)
        self._config_tags(self.ss_display)

    def _show_empty_thumb(self):
        for w in self.thumb_frame.winfo_children(): w.destroy()
        tk.Label(self.thumb_frame,text="No screenshots yet — press Alt+G to capture",bg=cfg.BG2,fg=cfg.FG2,font=cfg.FONT_SML).pack(padx=10,pady=22)

    def _refresh_thumbs(self):
        for w in self.thumb_frame.winfo_children(): w.destroy()
        if not self.screenshots: self._show_empty_thumb(); self.ss_count_var.set("0 / 3"); return
        self.ss_count_var.set(f"{len(self.screenshots)} / 3")
        for idx,photo in enumerate(self.ss_photos):
            card=tk.Frame(self.thumb_frame,bg=cfg.BG3); card.pack(side="left",padx=4,pady=4)
            tk.Label(card,image=photo,bg=cfg.BG3).pack()
            tk.Label(card,text=f"#{idx+1}",bg=cfg.BG3,fg=cfg.FG2,font=cfg.FONT_TINY).pack()
            xb=tk.Label(card,text="✕",bg=cfg.RED,fg="white",font=("Segoe UI",7,"bold"),cursor="",padx=3,pady=1)
            xb.place(relx=1.0,rely=0.0,anchor="ne",x=-1,y=1)
            xb.bind("<Button-1>",lambda e,i=idx:self._remove_screenshot(i))

    def _remove_screenshot(self,idx):
        if 0<=idx<len(self.screenshots):
            self.screenshots.pop(idx); self.ss_photos.pop(idx); self._refresh_thumbs()
            self._append(self.ss_display,"ai",f"Screenshot #{idx+1} removed.","System")

    def _switch_page(self,page):
        if page==self.current_page: return
        self.session.reset_session()
        self._clear_disp(self.chat_display); self._clear_disp(self.ss_display)
        self.screenshots.clear(); self.ss_photos.clear()
        if hasattr(self,"thumb_frame"): self._show_empty_thumb(); self.ss_count_var.set("0 / 3")
        target=self.chat_display if page=="chat" else self.ss_display
        self._append(target,"ai","🔄 Memory reset — new session started.","System")
        if page=="chat": self.show_chat_page()
        else: self.show_screenshot_page()

    def show_chat_page(self):
        self.ss_page.pack_forget(); self.chat_page.pack(fill="both",expand=True)
        self.current_page="chat"; self._activate_tab(self.tab_chat); self.chat_entry.focus_set()

    def show_screenshot_page(self):
        self.chat_page.pack_forget(); self.ss_page.pack(fill="both",expand=True)
        self.current_page="screenshot"; self._activate_tab(self.tab_ss)

    def _start_session(self):
        def _do():
            try:
                sid=self.session.start_session()
                self.root.after(0,lambda:self._set_status(f"● {sid[:8]}",cfg.GREEN))
                self.root.after(0,lambda:self._append(self.chat_display,"ai",
                    "👋 Hi! I'm your interview assistant.\nAsk me anything — coding, behavioral, system design.\n\n"
                    "Alt+Shift+S → Screenshot  │  Alt+H → Hide\nAlt+T → Click-through      │  Alt+S → Settings\n\n"
                    "Tip: Select any AI response text to copy it (Ctrl+C).","System"))
            except Exception:
                self.root.after(0,lambda:self._set_status("● offline",cfg.RED))
        threading.Thread(target=_do,daemon=True).start()

    def _on_enter(self,e):
        if e.state&0x1: return None
        self._send_chat(); return "break"

    def _send_chat(self):
        if not self.session.session_id or self.is_thinking: return
        msg=self.chat_entry.get("1.0","end").strip()
        if not msg: return
        self.chat_entry.delete("1.0","end")
        self._append(self.chat_display,"user",msg,"You")
        threading.Thread(target=self._do_chat,args=(msg,),daemon=True).start()

    def _do_chat(self,msg):
        self.is_thinking=True
        self.root.after(0,lambda:self._set_thinking(self.chat_display,True))
        try:
            data=self.groq.chat(msg,self.session)
            ans=data["answer"]; qt=data["question_type"]; mem=data["memory_depth"]
            self.root.after(0,lambda:self._set_thinking(self.chat_display,False))
            self.root.after(0,lambda:self._append(self.chat_display,"ai",ans,f"AI [{qt}] mem:{mem}/5"))
        except Exception as e:
            err_msg=str(e)
            self.root.after(0,lambda:self._set_thinking(self.chat_display,False))
            self.root.after(0,lambda:self._append(self.chat_display,"ai",f"Error: {err_msg}","Error"))
        finally:
            self.is_thinking=False

    def _capture_screenshot(self):
        if len(self.screenshots)>=cfg.MAX_SCREENSHOTS:
            self._append(self.ss_display,"ai","⚠ Max screenshots reached. Remove one first.","System"); return
        self.root.withdraw(); self.root.after(250,self._do_capture)

    def _do_capture(self):
        try:
            img=ImageGrab.grab(); b64=img_to_b64(img)
            self.screenshots.append(b64)
            thumb=img.copy(); thumb.thumbnail((118,66))
            self.ss_photos.append(ImageTk.PhotoImage(thumb))
            n=len(self.screenshots); self.root.deiconify()
            self.root.after(0,self._refresh_thumbs)
            self.root.after(0,lambda:self._append(self.ss_display,"ai",f"✅ Screenshot {n}/{cfg.MAX_SCREENSHOTS} captured.","System"))
        except Exception as e:
            err_msg=str(e)
            self.root.deiconify()
            self.root.after(0,lambda:self._append(self.ss_display,"ai",f"Capture error: {err_msg}","Error"))

    def _get_ss_answer(self):
        if not self.screenshots:
            self._append(self.ss_display,"ai","⚠ No screenshots. Press Alt+G first.","System"); return
        if self.is_thinking: return
        prompt=self.ss_prompt.get().strip() or None
        self._append(self.ss_display,"user",f"[{len(self.screenshots)} screenshot(s)]{(' — '+prompt) if prompt else ''}","You")
        threading.Thread(target=self._do_ss,args=(prompt,),daemon=True).start()

    def _do_ss(self,prompt):
        self.is_thinking=True
        self.root.after(0,lambda:self._set_thinking(self.ss_display,True))
        try:
            data=self.groq.analyze_screenshots(self.screenshots,prompt,self.session)
            ans=data["answer"]; qt=data["question_type"]; mem=data["memory_depth"]; shots=data["screenshots_analyzed"]
            self.root.after(0,lambda:self._set_thinking(self.ss_display,False))
            self.root.after(0,lambda:self._append(self.ss_display,"ai",ans,f"AI [{qt}] │ {shots} shot(s) │ mem:{mem}/5"))
            self.screenshots.clear(); self.ss_photos.clear()
            self.root.after(100,self._refresh_thumbs)
        except Exception as e:
            err_msg=str(e)
            self.root.after(0,lambda:self._set_thinking(self.ss_display,False))
            self.root.after(0,lambda:self._append(self.ss_display,"ai",f"Error: {err_msg}","Error"))
        finally:
            self.is_thinking=False

    def _config_tags(self,w):
        w.tag_configure("user_tag",background=cfg.USER_BG,foreground=cfg.FG,font=("Segoe UI",10,"bold"),lmargin1=8,lmargin2=8,rmargin=8,spacing1=4)
        w.tag_configure("ai_tag",background=cfg.AI_BG,foreground=cfg.FG,font=cfg.FONT_MONO,lmargin1=8,lmargin2=8,rmargin=8,spacing3=4)
        w.tag_configure("time_tag",foreground=cfg.FG2,font=("Segoe UI",7))
        w.tag_configure("thinking",foreground=cfg.YELLOW,font=("Segoe UI",9,"italic"))

    def _append(self,w,role,text,label=""):
        w.config(state="normal"); ts=datetime.now().strftime("%H:%M")
        if role=="user":
            w.insert("end",f"\n{label}  [{ts}]\n","user_tag"); w.insert("end",f"{text}\n","user_tag")
        else:
            w.insert("end",f"\n{label}  [{ts}]\n","time_tag"); w.insert("end",f"{text}\n","ai_tag")
        w.insert("end","\n"); w.config(state="disabled"); w.see("end")

    def _set_thinking(self,w,on):
        w.config(state="normal")
        if on: w.insert("end","\n⏳ Thinking…\n\n","thinking")
        else:
            c=w.get("1.0","end"); m="\n⏳ Thinking…\n\n"; p=c.rfind(m)
            if p!=-1: w.delete(f"1.0 + {p} chars",f"1.0 + {p+len(m)} chars")
        w.config(state="disabled"); w.see("end")

    def _clear_disp(self,w): w.config(state="normal"); w.delete("1.0","end"); w.config(state="disabled")
    def _clear_chat_display(self): self._clear_disp(self.chat_display)
    def _set_status(self,text,color=None): self.status_lbl.config(text=text,fg=color or cfg.GREEN)

    def _toggle_click_through(self):
        self.click_through=not self.click_through; set_click_through(self._hwnd,self.click_through)
        msg="Click-through ON" if self.click_through else "Click-through OFF"
        self._set_status(f"● {msg}",cfg.RED if self.click_through else cfg.GREEN)

    def _toggle_hide(self):
        if self._hidden: self.root.deiconify(); self.root.after(100,self._init_stealth); self._hidden=False
        else: self.root.withdraw(); self._hidden=True

    def _move_window(self,dx,dy):
        x=self.root.winfo_x()+dx; y=self.root.winfo_y()+dy; self.root.geometry(f"+{x}+{y}")

    def _register_hotkeys(self):
        def hk(c,f): keyboard.add_hotkey(c,lambda:self.root.after(0,f))
        hk("alt+shift+s",lambda:self._switch_page("screenshot"))
        hk("alt+b",lambda:self._switch_page("chat"))
        hk("alt+g",self._hk_capture); hk("alt+e",self._hk_submit)
        hk("alt+t",self._toggle_click_through); hk("alt+h",self._toggle_hide)
        hk("alt+q",self._quit); hk("alt+s",self._toggle_settings)
        hk("alt+up",lambda:self._move_window(0,-cfg.MOVE_STEP))
        hk("alt+down",lambda:self._move_window(0,cfg.MOVE_STEP))
        hk("alt+left",lambda:self._move_window(-cfg.MOVE_STEP,0))
        hk("alt+right",lambda:self._move_window(cfg.MOVE_STEP,0))

    def _hk_capture(self):
        if self.current_page=="screenshot": self._capture_screenshot()
    def _hk_submit(self):
        if self.current_page=="chat": self._send_chat()
        else: self._get_ss_answer()

    def _drag_start(self,e): self._drag_x=e.x; self._drag_y=e.y
    def _drag_motion(self,e):
        x=self.root.winfo_x()+(e.x-self._drag_x); y=self.root.winfo_y()+(e.y-self._drag_y)
        self.root.geometry(f"+{x}+{y}")

    def _quit(self):
        keyboard.unhook_all(); self.root.destroy()

if __name__=="__main__":
    InterviewAssistant()
