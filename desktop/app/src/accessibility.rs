//! AppKit semantic elements for GPUI's custom-drawn controls.
//!
//! GPUI 0.2 does not expose an accessibility tree. These markers use the actual
//! laid-out bounds, retain stable native elements across frames, and route AX
//! actions through the same UI thread as ordinary events. No mouse simulation.
use std::{cell::RefCell, collections::{BTreeMap, VecDeque}, rc::Rc, time::Duration};
use raw_window_handle::{HasWindowHandle, RawWindowHandle};
use gpui::{canvas, prelude::*, App, Bounds, Pixels, Window, WindowHandle};
use objc::{class, msg_send, sel, sel_impl, declare::ClassDecl, runtime::{Class, Object, Sel, BOOL, YES, NO}};

#[derive(Clone)]
pub enum Event { Press, Focus, SetText(String) }
type Handler = Rc<dyn Fn(Event, &mut Window, &mut App)>;
struct Node { object: *mut Object, handler: Handler, enabled: bool, generation: u64 }
#[derive(Default)]
struct Tree { nodes: BTreeMap<String, Node>, pending: VecDeque<(String, Event)>, generation: u64, active_scope: String, view: u64, window: usize }
thread_local! { static TREE: RefCell<Tree> = RefCell::new(Tree::default()); }

#[repr(C)]
#[derive(Clone, Copy)]
struct Rect { x: f64, y: f64, w: f64, h: f64 }
unsafe impl objc::Encode for Rect {
    fn encode() -> objc::Encoding { unsafe { objc::Encoding::from_str("{CGRect={CGPoint=dd}{CGSize=dd}}") } }
}
unsafe fn string(s: &str) -> *mut Object {
    let object: *mut Object = msg_send![class!(NSString), alloc];
    let object: *mut Object = msg_send![object, initWithBytes:s.as_ptr() length:s.len() encoding:4usize];
    msg_send![object, autorelease]
}
unsafe fn rust_string(s: *mut Object) -> String {
    if s.is_null() { return String::new(); }
    let bytes: *const std::os::raw::c_char = msg_send![s, UTF8String];
    if bytes.is_null() { String::new() } else { std::ffi::CStr::from_ptr(bytes).to_string_lossy().into_owned() }
}
fn enqueue(object: &Object, event: Event) -> BOOL {
    TREE.with(|tree| {
        let mut tree = tree.borrow_mut();
        let key = tree.nodes.iter().find(|(_, n)| std::ptr::eq(n.object, object) && n.enabled).map(|(key, _)| key.clone());
        if let Some(key) = key { tree.pending.push_back((key, event)); YES } else { NO }
    })
}
extern "C" fn press(object: &Object, _: Sel) -> BOOL { enqueue(object, Event::Press) }
extern "C" fn focus(object: &Object, _: Sel, focused: BOOL) { if focused == YES { enqueue(object, Event::Focus); } }
extern "C" fn set_value(object: &Object, _: Sel, value: *mut Object) { enqueue(object, Event::SetText(unsafe { rust_string(value) })); }
fn element_class(editable: bool) -> &'static Class {
    let name = if editable { "UmbrousAccessibleText" } else { "UmbrousAccessibleButton" };
    if let Some(class) = Class::get(name) { return class; }
    let mut decl = ClassDecl::new(name, class!(NSAccessibilityElement)).expect("accessibility class");
    unsafe {
        decl.add_method(sel!(accessibilityPerformPress), press as extern "C" fn(&Object, Sel) -> BOOL);
        decl.add_method(sel!(setAccessibilityFocused:), focus as extern "C" fn(&Object, Sel, BOOL));
        if editable { decl.add_method(sel!(setAccessibilityValue:), set_value as extern "C" fn(&Object, Sel, *mut Object)); }
    }
    decl.register()
}

/// Call once at the start of Workspace::render. Only markers painted in that
/// frame survive publication, so closed dialogs cannot leave actionable nodes.
impl Tree {
    fn start_frame(&mut self, scope: &str, view: u64) {
        if self.active_scope != scope || self.view != view { self.pending.clear(); }
        self.generation += 1;
        self.active_scope = scope.into();
        self.view = view;
    }
}
pub fn begin_frame(scope: &str, view: u64) { TREE.with(|tree| tree.borrow_mut().start_frame(scope, view)); }

/// Add as the last child of a relative-positioned control. It does not paint or
/// intercept input; its bounds track the control's real layout.
pub fn marker(id: impl Into<String>, label: impl Into<String>, value: Option<String>, enabled: bool,
              handler: impl Fn(Event, &mut Window, &mut App) + 'static) -> impl IntoElement {
    marker_in("main", id, label, value, enabled, handler)
}
pub fn marker_in(scope: &'static str, id: impl Into<String>, label: impl Into<String>, value: Option<String>, enabled: bool,
              handler: impl Fn(Event, &mut Window, &mut App) + 'static) -> impl IntoElement {
    let (id, label, handler) = (id.into(), label.into(), Rc::new(handler) as Handler);
    canvas(move |_, _, _| (), move |bounds, _, _, _| {
        if TREE.with(|tree| tree.borrow().active_scope == scope) { register(id, label, value, enabled, bounds, handler, false); }
    }).absolute().top_0().left_0().size_full()
}
pub fn text(scope: &'static str, id: impl Into<String>, label: impl Into<String>) -> impl IntoElement {
    let (id, label) = (id.into(), label.into());
    canvas(move |_, _, _| (), move |bounds, _, _, _| {
        if TREE.with(|tree| tree.borrow().active_scope == scope) {
            register(id, label, None, true, bounds, Rc::new(|_, _, _| {}), true);
        }
    }).absolute().top_0().left_0().size_full()
}
fn register(id: String, label: String, value: Option<String>, enabled: bool, bounds: Bounds<Pixels>, handler: Handler, static_text: bool) {
    unsafe {
        let win = TREE.with(|tree| tree.borrow().window as *mut Object);
        if win.is_null() { return; }
        let view: *mut Object = msg_send![win, contentView];
        let frame: Rect = msg_send![view, frame];
        let local = Rect { x: f32::from(bounds.origin.x) as f64, y: frame.h - f32::from(bounds.origin.y + bounds.size.height) as f64,
                           w: f32::from(bounds.size.width) as f64, h: f32::from(bounds.size.height) as f64 };
        let screen: Rect = msg_send![win, convertRectToScreen:local];
        TREE.with(|tree| {
            let mut tree = tree.borrow_mut();
            let generation = tree.generation;
            let node = tree.nodes.entry(id.clone()).or_insert_with(|| {
                let object: *mut Object = msg_send![if static_text { class!(NSAccessibilityElement) } else { element_class(value.is_some()) }, alloc];
                let object: *mut Object = msg_send![object, init];
                Node { object, handler: handler.clone(), enabled, generation }
            });
            node.handler = handler;
            node.enabled = enabled;
            node.generation = generation;
            let object = node.object;
            let _: () = msg_send![object, setAccessibilityRole:string(if static_text { "AXStaticText" } else if value.is_some() { "AXTextArea" } else { "AXButton" })];
            let _: () = msg_send![object, setAccessibilityLabel:string(&label)];
            let _: () = msg_send![object, setAccessibilityIdentifier:string(&id)];
            let _: () = msg_send![object, setAccessibilityParent:view];
            let _: () = msg_send![object, setAccessibilityFrame:screen];
            let _: () = msg_send![object, setAccessibilityEnabled:if enabled { YES } else { NO }];
            // Our editable subclass routes setAccessibilityValue to a UI event;
            // call the superclass setter when updating the exposed snapshot.
            if let Some(value) = value {
                let _: () = msg_send![super(object, class!(NSAccessibilityElement)), setAccessibilityValue:string(&value)];
            }
        });
    }
}

fn publish() {
    unsafe {
        let win = TREE.with(|tree| tree.borrow().window as *mut Object);
        if win.is_null() { return; }
        let view: *mut Object = msg_send![win, contentView];
        TREE.with(|tree| {
            let mut tree = tree.borrow_mut();
            let generation = tree.generation;
            let children: *mut Object = msg_send![class!(NSMutableArray), array];
            for node in tree.nodes.values().filter(|n| n.generation == generation) {
                let _: () = msg_send![children, addObject:node.object];
            }
            let _: () = msg_send![view, setAccessibilityElement:YES];
            let _: () = msg_send![view, setAccessibilityRole:string("AXGroup")];
            let _: () = msg_send![view, setAccessibilityLabel:string("Umbrous")];
            let _: () = msg_send![view, setAccessibilityChildren:children];
            tree.nodes.retain(|_, node| {
                if node.generation == generation { true } else { let _: () = msg_send![node.object, release]; false }
            });
        });
    }
}

pub fn install<T: 'static>(window: WindowHandle<T>, cx: &mut App) {
    window.update(cx, |_, window, _| {
        if let Ok(handle) = window.window_handle() {
            if let RawWindowHandle::AppKit(handle) = handle.as_raw() {
                let view = handle.ns_view.as_ptr() as *mut Object;
                let win: *mut Object = unsafe { msg_send![view, window] };
                TREE.with(|tree| tree.borrow_mut().window = win as usize);
            }
        }
    }).ok();
    cx.spawn(async move |cx| {
        loop {
            cx.background_executor().timer(Duration::from_millis(50)).await;
            let work = TREE.with(|tree| {
                let mut tree = tree.borrow_mut();
                let pending = tree.pending.pop_front();
                // Process one command per frame. Ordinary typing/focus events
                // remain queued, but a chat/modal transition clears old actions.
                pending.into_iter().filter_map(|(key, event)| tree.nodes.get(&key)
                    .filter(|n| n.enabled && n.generation == tree.generation)
                    .map(|n| (n.handler.clone(), event))).collect::<Vec<_>>()
            });
            if window.update(cx, |_, window, cx| {
                for (handler, event) in work { handler(event, window, cx); }
                publish();
            }).is_err() {
                TREE.with(|tree| {
                    let mut tree = tree.borrow_mut();
                    for node in tree.nodes.values() { unsafe { let _: () = msg_send![node.object, release]; } }
                    *tree = Tree::default();
                });
                break;
            }
        }
    }).detach();
}

/// Bind one handler to mouse activation and AppKit AXPress, keeping permission
/// checks and state transitions identical across input methods.
pub trait AccessibleClick: Sized {
    fn accessible_click_in(self, scope: &'static str, label: impl Into<String>, enabled: bool,
        handler: impl Fn(&gpui::ClickEvent, &mut Window, &mut App) + 'static) -> Self;
    fn accessible_click(self, label: impl Into<String>, enabled: bool,
        handler: impl Fn(&gpui::ClickEvent, &mut Window, &mut App) + 'static) -> Self;
}
impl AccessibleClick for gpui::Stateful<gpui::Div> {
    fn accessible_click(self, label: impl Into<String>, enabled: bool,
        handler: impl Fn(&gpui::ClickEvent, &mut Window, &mut App) + 'static) -> Self {
        self.accessible_click_in("main", label, enabled, handler)
    }
    fn accessible_click_in(mut self, scope: &'static str, label: impl Into<String>, enabled: bool,
        handler: impl Fn(&gpui::ClickEvent, &mut Window, &mut App) + 'static) -> Self {
        let id = format!("{:?}", self.interactivity().element_id);
        let handler = Rc::new(handler);
        let native = handler.clone();
        self.relative().child(marker_in(scope, id, label, None, enabled, move |event, window, cx| {
            if matches!(event, Event::Press) { native(&gpui::ClickEvent::default(), window, cx); }
        })).on_click(move |event, window, cx| handler(event, window, cx))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ordinary_redraw_preserves_pending_text_entry() {
        let mut tree = Tree::default();
        tree.start_frame("main", 1);
        tree.pending.push_back(("composer".into(), Event::Focus));
        tree.pending.push_back(("composer".into(), Event::SetText("draft".into())));
        tree.start_frame("main", 1);
        assert_eq!(tree.pending.len(), 2);
        assert!(matches!(tree.pending.pop_front(), Some((_, Event::Focus))));
        assert!(matches!(tree.pending.pop_front(), Some((_, Event::SetText(text))) if text == "draft"));
    }
    #[test]
    fn opening_modal_discards_queued_background_actions() {
        let mut tree = Tree::default();
        tree.start_frame("main", 1);
        tree.pending.push_back(("send".into(), Event::Press));
        tree.start_frame("settings", 1);
        assert!(tree.pending.is_empty());
    }
    #[test]
    fn switching_chat_discards_actions_for_previous_composer() {
        let mut tree = Tree::default();
        tree.start_frame("main", 1);
        tree.pending.push_back(("composer".into(), Event::SetText("old draft".into())));
        tree.start_frame("main", 2);
        assert!(tree.pending.is_empty());
    }
}
