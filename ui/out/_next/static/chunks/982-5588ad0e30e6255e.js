'use strict';
(self.webpackChunk_N_E = self.webpackChunk_N_E || []).push([
  [982],
  {
    718: (e, t, r) => {
      r.d(t, { RG: () => g, bL: () => F, q7: () => D });
      var n = r(4398),
        a = r(6687),
        o = r(6383),
        i = r(2050),
        l = r(940),
        s = r(6565),
        c = r(3780),
        u = r(7589),
        d = r(6657),
        f = r(7689),
        p = r(3422),
        v = 'rovingFocusGroup.onEntryFocus',
        h = { bubbles: !1, cancelable: !0 },
        b = 'RovingFocusGroup',
        [m, y, w] = (0, o.N)(b),
        [x, g] = (0, l.A)(b, [w]),
        [k, R] = x(b),
        j = n.forwardRef((e, t) =>
          (0, p.jsx)(m.Provider, {
            scope: e.__scopeRovingFocusGroup,
            children: (0, p.jsx)(m.Slot, {
              scope: e.__scopeRovingFocusGroup,
              children: (0, p.jsx)(A, { ...e, ref: t }),
            }),
          })
        );
      j.displayName = b;
      var A = n.forwardRef((e, t) => {
          let {
              __scopeRovingFocusGroup: r,
              orientation: o,
              loop: l = !1,
              dir: s,
              currentTabStopId: m,
              defaultCurrentTabStopId: w,
              onCurrentTabStopIdChange: x,
              onEntryFocus: g,
              preventScrollOnEntryFocus: R = !1,
              ...j
            } = e,
            A = n.useRef(null),
            C = (0, i.s)(t, A),
            I = (0, f.jH)(s),
            [K, F] = (0, d.i)({
              prop: m,
              defaultProp: null != w ? w : null,
              onChange: x,
              caller: b,
            }),
            [D, T] = n.useState(!1),
            G = (0, u.c)(g),
            M = y(r),
            S = n.useRef(!1),
            [L, N] = n.useState(0);
          return (
            n.useEffect(() => {
              let e = A.current;
              if (e)
                return (
                  e.addEventListener(v, G),
                  () => e.removeEventListener(v, G)
                );
            }, [G]),
            (0, p.jsx)(k, {
              scope: r,
              orientation: o,
              dir: I,
              loop: l,
              currentTabStopId: K,
              onItemFocus: n.useCallback((e) => F(e), [F]),
              onItemShiftTab: n.useCallback(() => T(!0), []),
              onFocusableItemAdd: n.useCallback(() => N((e) => e + 1), []),
              onFocusableItemRemove: n.useCallback(() => N((e) => e - 1), []),
              children: (0, p.jsx)(c.sG.div, {
                tabIndex: D || 0 === L ? -1 : 0,
                'data-orientation': o,
                ...j,
                ref: C,
                style: { outline: 'none', ...e.style },
                onMouseDown: (0, a.mK)(e.onMouseDown, () => {
                  S.current = !0;
                }),
                onFocus: (0, a.mK)(e.onFocus, (e) => {
                  let t = !S.current;
                  if (e.target === e.currentTarget && t && !D) {
                    let t = new CustomEvent(v, h);
                    if (
                      (e.currentTarget.dispatchEvent(t), !t.defaultPrevented)
                    ) {
                      let e = M().filter((e) => e.focusable);
                      E(
                        [
                          e.find((e) => e.active),
                          e.find((e) => e.id === K),
                          ...e,
                        ]
                          .filter(Boolean)
                          .map((e) => e.ref.current),
                        R
                      );
                    }
                  }
                  S.current = !1;
                }),
                onBlur: (0, a.mK)(e.onBlur, () => T(!1)),
              }),
            })
          );
        }),
        C = 'RovingFocusGroupItem',
        I = n.forwardRef((e, t) => {
          let {
              __scopeRovingFocusGroup: r,
              focusable: o = !0,
              active: i = !1,
              tabStopId: l,
              children: u,
              ...d
            } = e,
            f = (0, s.B)(),
            v = l || f,
            h = R(C, r),
            b = h.currentTabStopId === v,
            w = y(r),
            {
              onFocusableItemAdd: x,
              onFocusableItemRemove: g,
              currentTabStopId: k,
            } = h;
          return (
            n.useEffect(() => {
              if (o) return (x(), () => g());
            }, [o, x, g]),
            (0, p.jsx)(m.ItemSlot, {
              scope: r,
              id: v,
              focusable: o,
              active: i,
              children: (0, p.jsx)(c.sG.span, {
                tabIndex: b ? 0 : -1,
                'data-orientation': h.orientation,
                ...d,
                ref: t,
                onMouseDown: (0, a.mK)(e.onMouseDown, (e) => {
                  o ? h.onItemFocus(v) : e.preventDefault();
                }),
                onFocus: (0, a.mK)(e.onFocus, () => h.onItemFocus(v)),
                onKeyDown: (0, a.mK)(e.onKeyDown, (e) => {
                  if ('Tab' === e.key && e.shiftKey)
                    return void h.onItemShiftTab();
                  if (e.target !== e.currentTarget) return;
                  let t = (function (e, t, r) {
                    var n;
                    let a =
                      ((n = e.key),
                      'rtl' !== r
                        ? n
                        : 'ArrowLeft' === n
                          ? 'ArrowRight'
                          : 'ArrowRight' === n
                            ? 'ArrowLeft'
                            : n);
                    if (
                      !(
                        'vertical' === t &&
                        ['ArrowLeft', 'ArrowRight'].includes(a)
                      ) &&
                      !(
                        'horizontal' === t &&
                        ['ArrowUp', 'ArrowDown'].includes(a)
                      )
                    )
                      return K[a];
                  })(e, h.orientation, h.dir);
                  if (void 0 !== t) {
                    if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey)
                      return;
                    e.preventDefault();
                    let r = w()
                      .filter((e) => e.focusable)
                      .map((e) => e.ref.current);
                    if ('last' === t) r.reverse();
                    else if ('prev' === t || 'next' === t) {
                      'prev' === t && r.reverse();
                      let n = r.indexOf(e.currentTarget);
                      r = h.loop
                        ? (function (e, t) {
                            return e.map((r, n) => e[(t + n) % e.length]);
                          })(r, n + 1)
                        : r.slice(n + 1);
                    }
                    setTimeout(() => E(r));
                  }
                }),
                children:
                  'function' == typeof u
                    ? u({ isCurrentTabStop: b, hasTabStop: null != k })
                    : u,
              }),
            })
          );
        });
      I.displayName = C;
      var K = {
        ArrowLeft: 'prev',
        ArrowUp: 'prev',
        ArrowRight: 'next',
        ArrowDown: 'next',
        PageUp: 'first',
        Home: 'first',
        PageDown: 'last',
        End: 'last',
      };
      function E(e) {
        let t = arguments.length > 1 && void 0 !== arguments[1] && arguments[1],
          r = document.activeElement;
        for (let n of e)
          if (
            n === r ||
            (n.focus({ preventScroll: t }), document.activeElement !== r)
          )
            return;
      }
      var F = j,
        D = I;
    },
    856: (e, t, r) => {
      r.d(t, { A: () => n });
      let n = (0, r(3929).A)('plus', [
        ['path', { d: 'M5 12h14', key: '1ays0h' }],
        ['path', { d: 'M12 5v14', key: 's699le' }],
      ]);
    },
    1749: (e, t, r) => {
      r.d(t, { B8: () => E, UC: () => D, bL: () => K, l9: () => F });
      var n = r(4398),
        a = r(6687),
        o = r(940),
        i = r(718),
        l = r(6175),
        s = r(3780),
        c = r(7689),
        u = r(6657),
        d = r(6565),
        f = r(3422),
        p = 'Tabs',
        [v, h] = (0, o.A)(p, [i.RG]),
        b = (0, i.RG)(),
        [m, y] = v(p),
        w = n.forwardRef((e, t) => {
          let {
              __scopeTabs: r,
              value: n,
              onValueChange: a,
              defaultValue: o,
              orientation: i = 'horizontal',
              dir: l,
              activationMode: v = 'automatic',
              ...h
            } = e,
            b = (0, c.jH)(l),
            [y, w] = (0, u.i)({
              prop: n,
              onChange: a,
              defaultProp: null != o ? o : '',
              caller: p,
            });
          return (0, f.jsx)(m, {
            scope: r,
            baseId: (0, d.B)(),
            value: y,
            onValueChange: w,
            orientation: i,
            dir: b,
            activationMode: v,
            children: (0, f.jsx)(s.sG.div, {
              dir: b,
              'data-orientation': i,
              ...h,
              ref: t,
            }),
          });
        });
      w.displayName = p;
      var x = 'TabsList',
        g = n.forwardRef((e, t) => {
          let { __scopeTabs: r, loop: n = !0, ...a } = e,
            o = y(x, r),
            l = b(r);
          return (0, f.jsx)(i.bL, {
            asChild: !0,
            ...l,
            orientation: o.orientation,
            dir: o.dir,
            loop: n,
            children: (0, f.jsx)(s.sG.div, {
              role: 'tablist',
              'aria-orientation': o.orientation,
              ...a,
              ref: t,
            }),
          });
        });
      g.displayName = x;
      var k = 'TabsTrigger',
        R = n.forwardRef((e, t) => {
          let { __scopeTabs: r, value: n, disabled: o = !1, ...l } = e,
            c = y(k, r),
            u = b(r),
            d = C(c.baseId, n),
            p = I(c.baseId, n),
            v = n === c.value;
          return (0, f.jsx)(i.q7, {
            asChild: !0,
            ...u,
            focusable: !o,
            active: v,
            children: (0, f.jsx)(s.sG.button, {
              type: 'button',
              role: 'tab',
              'aria-selected': v,
              'aria-controls': p,
              'data-state': v ? 'active' : 'inactive',
              'data-disabled': o ? '' : void 0,
              disabled: o,
              id: d,
              ...l,
              ref: t,
              onMouseDown: (0, a.mK)(e.onMouseDown, (e) => {
                o || 0 !== e.button || !1 !== e.ctrlKey
                  ? e.preventDefault()
                  : c.onValueChange(n);
              }),
              onKeyDown: (0, a.mK)(e.onKeyDown, (e) => {
                [' ', 'Enter'].includes(e.key) && c.onValueChange(n);
              }),
              onFocus: (0, a.mK)(e.onFocus, () => {
                let e = 'manual' !== c.activationMode;
                v || o || !e || c.onValueChange(n);
              }),
            }),
          });
        });
      R.displayName = k;
      var j = 'TabsContent',
        A = n.forwardRef((e, t) => {
          let {
              __scopeTabs: r,
              value: a,
              forceMount: o,
              children: i,
              ...c
            } = e,
            u = y(j, r),
            d = C(u.baseId, a),
            p = I(u.baseId, a),
            v = a === u.value,
            h = n.useRef(v);
          return (
            n.useEffect(() => {
              let e = requestAnimationFrame(() => (h.current = !1));
              return () => cancelAnimationFrame(e);
            }, []),
            (0, f.jsx)(l.C, {
              present: o || v,
              children: (r) => {
                let { present: n } = r;
                return (0, f.jsx)(s.sG.div, {
                  'data-state': v ? 'active' : 'inactive',
                  'data-orientation': u.orientation,
                  role: 'tabpanel',
                  'aria-labelledby': d,
                  hidden: !n,
                  id: p,
                  tabIndex: 0,
                  ...c,
                  ref: t,
                  style: {
                    ...e.style,
                    animationDuration: h.current ? '0s' : void 0,
                  },
                  children: n && i,
                });
              },
            })
          );
        });
      function C(e, t) {
        return ''.concat(e, '-trigger-').concat(t);
      }
      function I(e, t) {
        return ''.concat(e, '-content-').concat(t);
      }
      A.displayName = j;
      var K = w,
        E = g,
        F = R,
        D = A;
    },
    2495: (e, t, r) => {
      r.d(t, { bL: () => k, zi: () => R });
      var n = r(4398),
        a = r(6687),
        o = r(2050),
        i = r(940),
        l = r(6657),
        s = r(6017),
        c = r(4753),
        u = r(3780),
        d = r(3422),
        f = 'Switch',
        [p, v] = (0, i.A)(f),
        [h, b] = p(f),
        m = n.forwardRef((e, t) => {
          let {
              __scopeSwitch: r,
              name: i,
              checked: s,
              defaultChecked: c,
              required: p,
              disabled: v,
              value: b = 'on',
              onCheckedChange: m,
              form: y,
              ...w
            } = e,
            [k, R] = n.useState(null),
            j = (0, o.s)(t, (e) => R(e)),
            A = n.useRef(!1),
            C = !k || y || !!k.closest('form'),
            [I, K] = (0, l.i)({
              prop: s,
              defaultProp: null != c && c,
              onChange: m,
              caller: f,
            });
          return (0, d.jsxs)(h, {
            scope: r,
            checked: I,
            disabled: v,
            children: [
              (0, d.jsx)(u.sG.button, {
                type: 'button',
                role: 'switch',
                'aria-checked': I,
                'aria-required': p,
                'data-state': g(I),
                'data-disabled': v ? '' : void 0,
                disabled: v,
                value: b,
                ...w,
                ref: j,
                onClick: (0, a.mK)(e.onClick, (e) => {
                  (K((e) => !e),
                    C &&
                      ((A.current = e.isPropagationStopped()),
                      A.current || e.stopPropagation()));
                }),
              }),
              C &&
                (0, d.jsx)(x, {
                  control: k,
                  bubbles: !A.current,
                  name: i,
                  value: b,
                  checked: I,
                  required: p,
                  disabled: v,
                  form: y,
                  style: { transform: 'translateX(-100%)' },
                }),
            ],
          });
        });
      m.displayName = f;
      var y = 'SwitchThumb',
        w = n.forwardRef((e, t) => {
          let { __scopeSwitch: r, ...n } = e,
            a = b(y, r);
          return (0, d.jsx)(u.sG.span, {
            'data-state': g(a.checked),
            'data-disabled': a.disabled ? '' : void 0,
            ...n,
            ref: t,
          });
        });
      w.displayName = y;
      var x = n.forwardRef((e, t) => {
        let {
            __scopeSwitch: r,
            control: a,
            checked: i,
            bubbles: l = !0,
            ...u
          } = e,
          f = n.useRef(null),
          p = (0, o.s)(f, t),
          v = (0, s.Z)(i),
          h = (0, c.X)(a);
        return (
          n.useEffect(() => {
            let e = f.current;
            if (!e) return;
            let t = Object.getOwnPropertyDescriptor(
              window.HTMLInputElement.prototype,
              'checked'
            ).set;
            if (v !== i && t) {
              let r = new Event('click', { bubbles: l });
              (t.call(e, i), e.dispatchEvent(r));
            }
          }, [v, i, l]),
          (0, d.jsx)('input', {
            type: 'checkbox',
            'aria-hidden': !0,
            defaultChecked: i,
            ...u,
            tabIndex: -1,
            ref: p,
            style: {
              ...u.style,
              ...h,
              position: 'absolute',
              pointerEvents: 'none',
              opacity: 0,
              margin: 0,
            },
          })
        );
      });
      function g(e) {
        return e ? 'checked' : 'unchecked';
      }
      x.displayName = 'SwitchBubbleInput';
      var k = m,
        R = w;
    },
    6541: (e, t, r) => {
      r.d(t, { A: () => n });
      let n = (0, r(3929).A)('trash-2', [
        ['path', { d: 'M3 6h18', key: 'd0wm0j' }],
        ['path', { d: 'M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6', key: '4alrt4' }],
        ['path', { d: 'M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2', key: 'v07s0e' }],
        ['line', { x1: '10', x2: '10', y1: '11', y2: '17', key: '1uufr5' }],
        ['line', { x1: '14', x2: '14', y1: '11', y2: '17', key: 'xtxkd' }],
      ]);
    },
    7137: (e, t, r) => {
      r.d(t, { A: () => n });
      let n = (0, r(3929).A)('eye', [
        [
          'path',
          {
            d: 'M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0',
            key: '1nclc0',
          },
        ],
        ['circle', { cx: '12', cy: '12', r: '3', key: '1v7zrd' }],
      ]);
    },
  },
]);
