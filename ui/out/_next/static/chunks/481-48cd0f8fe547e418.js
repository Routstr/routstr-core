'use strict';
(self.webpackChunk_N_E = self.webpackChunk_N_E || []).push([
  [481],
  {
    1331: (e, t, r) => {
      r.d(t, { $: () => a, s: () => s });
      var n = r(5688),
        i = r(449),
        o = r(2925),
        s = class extends i.k {
          #e;
          #t;
          #r;
          #n;
          constructor(e) {
            (super(),
              (this.#e = e.client),
              (this.mutationId = e.mutationId),
              (this.#r = e.mutationCache),
              (this.#t = []),
              (this.state = e.state || a()),
              this.setOptions(e.options),
              this.scheduleGc());
          }
          setOptions(e) {
            ((this.options = e), this.updateGcTime(this.options.gcTime));
          }
          get meta() {
            return this.options.meta;
          }
          addObserver(e) {
            this.#t.includes(e) ||
              (this.#t.push(e),
              this.clearGcTimeout(),
              this.#r.notify({
                type: 'observerAdded',
                mutation: this,
                observer: e,
              }));
          }
          removeObserver(e) {
            ((this.#t = this.#t.filter((t) => t !== e)),
              this.scheduleGc(),
              this.#r.notify({
                type: 'observerRemoved',
                mutation: this,
                observer: e,
              }));
          }
          optionalRemove() {
            this.#t.length ||
              ('pending' === this.state.status
                ? this.scheduleGc()
                : this.#r.remove(this));
          }
          continue() {
            return this.#n?.continue() ?? this.execute(this.state.variables);
          }
          async execute(e) {
            let t = () => {
                this.#i({ type: 'continue' });
              },
              r = {
                client: this.#e,
                meta: this.options.meta,
                mutationKey: this.options.mutationKey,
              };
            this.#n = (0, o.II)({
              fn: () =>
                this.options.mutationFn
                  ? this.options.mutationFn(e, r)
                  : Promise.reject(Error('No mutationFn found')),
              onFail: (e, t) => {
                this.#i({ type: 'failed', failureCount: e, error: t });
              },
              onPause: () => {
                this.#i({ type: 'pause' });
              },
              onContinue: t,
              retry: this.options.retry ?? 0,
              retryDelay: this.options.retryDelay,
              networkMode: this.options.networkMode,
              canRun: () => this.#r.canRun(this),
            });
            let n = 'pending' === this.state.status,
              i = !this.#n.canStart();
            try {
              if (n) t();
              else {
                (this.#i({ type: 'pending', variables: e, isPaused: i }),
                  await this.#r.config.onMutate?.(e, this, r));
                let t = await this.options.onMutate?.(e, r);
                t !== this.state.context &&
                  this.#i({
                    type: 'pending',
                    context: t,
                    variables: e,
                    isPaused: i,
                  });
              }
              let o = await this.#n.start();
              return (
                await this.#r.config.onSuccess?.(
                  o,
                  e,
                  this.state.context,
                  this,
                  r
                ),
                await this.options.onSuccess?.(o, e, this.state.context, r),
                await this.#r.config.onSettled?.(
                  o,
                  null,
                  this.state.variables,
                  this.state.context,
                  this,
                  r
                ),
                await this.options.onSettled?.(
                  o,
                  null,
                  e,
                  this.state.context,
                  r
                ),
                this.#i({ type: 'success', data: o }),
                o
              );
            } catch (t) {
              try {
                throw (
                  await this.#r.config.onError?.(
                    t,
                    e,
                    this.state.context,
                    this,
                    r
                  ),
                  await this.options.onError?.(t, e, this.state.context, r),
                  await this.#r.config.onSettled?.(
                    void 0,
                    t,
                    this.state.variables,
                    this.state.context,
                    this,
                    r
                  ),
                  await this.options.onSettled?.(
                    void 0,
                    t,
                    e,
                    this.state.context,
                    r
                  ),
                  t
                );
              } finally {
                this.#i({ type: 'error', error: t });
              }
            } finally {
              this.#r.runNext(this);
            }
          }
          #i(e) {
            ((this.state = ((t) => {
              switch (e.type) {
                case 'failed':
                  return {
                    ...t,
                    failureCount: e.failureCount,
                    failureReason: e.error,
                  };
                case 'pause':
                  return { ...t, isPaused: !0 };
                case 'continue':
                  return { ...t, isPaused: !1 };
                case 'pending':
                  return {
                    ...t,
                    context: e.context,
                    data: void 0,
                    failureCount: 0,
                    failureReason: null,
                    error: null,
                    isPaused: e.isPaused,
                    status: 'pending',
                    variables: e.variables,
                    submittedAt: Date.now(),
                  };
                case 'success':
                  return {
                    ...t,
                    data: e.data,
                    failureCount: 0,
                    failureReason: null,
                    error: null,
                    status: 'success',
                    isPaused: !1,
                  };
                case 'error':
                  return {
                    ...t,
                    data: void 0,
                    error: e.error,
                    failureCount: t.failureCount + 1,
                    failureReason: e.error,
                    isPaused: !1,
                    status: 'error',
                  };
              }
            })(this.state)),
              n.jG.batch(() => {
                (this.#t.forEach((t) => {
                  t.onMutationUpdate(e);
                }),
                  this.#r.notify({
                    mutation: this,
                    type: 'updated',
                    action: e,
                  }));
              }));
          }
        };
      function a() {
        return {
          context: void 0,
          data: void 0,
          error: null,
          failureCount: 0,
          failureReason: null,
          isPaused: !1,
          status: 'idle',
          variables: void 0,
          submittedAt: 0,
        };
      }
    },
    1537: (e, t, r) => {
      r.d(t, { q: () => n });
      function n(e, [t, r]) {
        return Math.min(r, Math.max(t, e));
      }
    },
    3439: (e, t, r) => {
      r.d(t, {
        In: () => eT,
        LM: () => eK,
        PP: () => eA,
        UC: () => eI,
        VF: () => eL,
        WT: () => eP,
        ZL: () => eN,
        bL: () => ek,
        l9: () => eE,
        p4: () => eO,
        q7: () => eD,
        wn: () => eG,
      });
      var n = r(4398),
        i = r(5707),
        o = r(1537),
        s = r(6687),
        a = r(6383),
        l = r(2050),
        u = r(940),
        c = r(7689),
        d = r(3213),
        h = r(7),
        p = r(3138),
        f = r(6565),
        v = r(6387),
        m = r(1732),
        y = r(3780),
        g = r(6950),
        w = r(7589),
        x = r(6657),
        b = r(3177),
        C = r(6017),
        S = r(1581),
        R = r(3871),
        M = r(3338),
        j = r(3422),
        k = [' ', 'Enter', 'ArrowUp', 'ArrowDown'],
        E = [' ', 'Enter'],
        P = 'Select',
        [T, N, I] = (0, a.N)(P),
        [K, D] = (0, u.A)(P, [I, v.Bk]),
        O = (0, v.Bk)(),
        [L, A] = K(P),
        [G, H] = K(P),
        _ = (e) => {
          let {
              __scopeSelect: t,
              children: r,
              open: i,
              defaultOpen: o,
              onOpenChange: s,
              value: a,
              defaultValue: l,
              onValueChange: u,
              dir: d,
              name: h,
              autoComplete: p,
              disabled: m,
              required: y,
              form: g,
            } = e,
            w = O(t),
            [b, C] = n.useState(null),
            [S, R] = n.useState(null),
            [M, k] = n.useState(!1),
            E = (0, c.jH)(d),
            [N, I] = (0, x.i)({
              prop: i,
              defaultProp: null != o && o,
              onChange: s,
              caller: P,
            }),
            [K, D] = (0, x.i)({
              prop: a,
              defaultProp: l,
              onChange: u,
              caller: P,
            }),
            A = n.useRef(null),
            H = !b || g || !!b.closest('form'),
            [_, B] = n.useState(new Set()),
            F = Array.from(_)
              .map((e) => e.props.value)
              .join(';');
          return (0, j.jsx)(v.bL, {
            ...w,
            children: (0, j.jsxs)(L, {
              required: y,
              scope: t,
              trigger: b,
              onTriggerChange: C,
              valueNode: S,
              onValueNodeChange: R,
              valueNodeHasChildren: M,
              onValueNodeHasChildrenChange: k,
              contentId: (0, f.B)(),
              value: K,
              onValueChange: D,
              open: N,
              onOpenChange: I,
              dir: E,
              triggerPointerDownPosRef: A,
              disabled: m,
              children: [
                (0, j.jsx)(T.Provider, {
                  scope: t,
                  children: (0, j.jsx)(G, {
                    scope: e.__scopeSelect,
                    onNativeOptionAdd: n.useCallback((e) => {
                      B((t) => new Set(t).add(e));
                    }, []),
                    onNativeOptionRemove: n.useCallback((e) => {
                      B((t) => {
                        let r = new Set(t);
                        return (r.delete(e), r);
                      });
                    }, []),
                    children: r,
                  }),
                }),
                H
                  ? (0, j.jsxs)(
                      eS,
                      {
                        'aria-hidden': !0,
                        required: y,
                        tabIndex: -1,
                        name: h,
                        autoComplete: p,
                        value: K,
                        onChange: (e) => D(e.target.value),
                        disabled: m,
                        form: g,
                        children: [
                          void 0 === K
                            ? (0, j.jsx)('option', { value: '' })
                            : null,
                          Array.from(_),
                        ],
                      },
                      F
                    )
                  : null,
              ],
            }),
          });
        };
      _.displayName = P;
      var B = 'SelectTrigger',
        F = n.forwardRef((e, t) => {
          let { __scopeSelect: r, disabled: i = !1, ...o } = e,
            a = O(r),
            u = A(B, r),
            c = u.disabled || i,
            d = (0, l.s)(t, u.onTriggerChange),
            h = N(r),
            p = n.useRef('touch'),
            [f, m, g] = eM((e) => {
              let t = h().filter((e) => !e.disabled),
                r = t.find((e) => e.value === u.value),
                n = ej(t, e, r);
              void 0 !== n && u.onValueChange(n.value);
            }),
            w = (e) => {
              (c || (u.onOpenChange(!0), g()),
                e &&
                  (u.triggerPointerDownPosRef.current = {
                    x: Math.round(e.pageX),
                    y: Math.round(e.pageY),
                  }));
            };
          return (0, j.jsx)(v.Mz, {
            asChild: !0,
            ...a,
            children: (0, j.jsx)(y.sG.button, {
              type: 'button',
              role: 'combobox',
              'aria-controls': u.contentId,
              'aria-expanded': u.open,
              'aria-required': u.required,
              'aria-autocomplete': 'none',
              dir: u.dir,
              'data-state': u.open ? 'open' : 'closed',
              disabled: c,
              'data-disabled': c ? '' : void 0,
              'data-placeholder': eR(u.value) ? '' : void 0,
              ...o,
              ref: d,
              onClick: (0, s.mK)(o.onClick, (e) => {
                (e.currentTarget.focus(), 'mouse' !== p.current && w(e));
              }),
              onPointerDown: (0, s.mK)(o.onPointerDown, (e) => {
                p.current = e.pointerType;
                let t = e.target;
                (t.hasPointerCapture(e.pointerId) &&
                  t.releasePointerCapture(e.pointerId),
                  0 === e.button &&
                    !1 === e.ctrlKey &&
                    'mouse' === e.pointerType &&
                    (w(e), e.preventDefault()));
              }),
              onKeyDown: (0, s.mK)(o.onKeyDown, (e) => {
                let t = '' !== f.current;
                (e.ctrlKey ||
                  e.altKey ||
                  e.metaKey ||
                  1 !== e.key.length ||
                  m(e.key),
                  (!t || ' ' !== e.key) &&
                    k.includes(e.key) &&
                    (w(), e.preventDefault()));
              }),
            }),
          });
        });
      F.displayName = B;
      var V = 'SelectValue',
        U = n.forwardRef((e, t) => {
          let {
              __scopeSelect: r,
              className: n,
              style: i,
              children: o,
              placeholder: s = '',
              ...a
            } = e,
            u = A(V, r),
            { onValueNodeHasChildrenChange: c } = u,
            d = void 0 !== o,
            h = (0, l.s)(t, u.onValueNodeChange);
          return (
            (0, b.N)(() => {
              c(d);
            }, [c, d]),
            (0, j.jsx)(y.sG.span, {
              ...a,
              ref: h,
              style: { pointerEvents: 'none' },
              children: eR(u.value)
                ? (0, j.jsx)(j.Fragment, { children: s })
                : o,
            })
          );
        });
      U.displayName = V;
      var W = n.forwardRef((e, t) => {
        let { __scopeSelect: r, children: n, ...i } = e;
        return (0, j.jsx)(y.sG.span, {
          'aria-hidden': !0,
          ...i,
          ref: t,
          children: n || '▼',
        });
      });
      W.displayName = 'SelectIcon';
      var q = (e) => (0, j.jsx)(m.Z, { asChild: !0, ...e });
      q.displayName = 'SelectPortal';
      var z = 'SelectContent',
        Z = n.forwardRef((e, t) => {
          let r = A(z, e.__scopeSelect),
            [o, s] = n.useState();
          return ((0, b.N)(() => {
            s(new DocumentFragment());
          }, []),
          r.open)
            ? (0, j.jsx)($, { ...e, ref: t })
            : o
              ? i.createPortal(
                  (0, j.jsx)(Q, {
                    scope: e.__scopeSelect,
                    children: (0, j.jsx)(T.Slot, {
                      scope: e.__scopeSelect,
                      children: (0, j.jsx)('div', { children: e.children }),
                    }),
                  }),
                  o
                )
              : null;
        });
      Z.displayName = z;
      var [Q, X] = K(z),
        Y = (0, g.TL)('SelectContent.RemoveScroll'),
        $ = n.forwardRef((e, t) => {
          let {
              __scopeSelect: r,
              position: i = 'item-aligned',
              onCloseAutoFocus: o,
              onEscapeKeyDown: a,
              onPointerDownOutside: u,
              side: c,
              sideOffset: f,
              align: v,
              alignOffset: m,
              arrowPadding: y,
              collisionBoundary: g,
              collisionPadding: w,
              sticky: x,
              hideWhenDetached: b,
              avoidCollisions: C,
              ...S
            } = e,
            k = A(z, r),
            [E, P] = n.useState(null),
            [T, I] = n.useState(null),
            K = (0, l.s)(t, (e) => P(e)),
            [D, O] = n.useState(null),
            [L, G] = n.useState(null),
            H = N(r),
            [_, B] = n.useState(!1),
            F = n.useRef(!1);
          (n.useEffect(() => {
            if (E) return (0, R.Eq)(E);
          }, [E]),
            (0, h.Oh)());
          let V = n.useCallback(
              (e) => {
                let [t, ...r] = H().map((e) => e.ref.current),
                  [n] = r.slice(-1),
                  i = document.activeElement;
                for (let r of e)
                  if (
                    r === i ||
                    (null == r || r.scrollIntoView({ block: 'nearest' }),
                    r === t && T && (T.scrollTop = 0),
                    r === n && T && (T.scrollTop = T.scrollHeight),
                    null == r || r.focus(),
                    document.activeElement !== i)
                  )
                    return;
              },
              [H, T]
            ),
            U = n.useCallback(() => V([D, E]), [V, D, E]);
          n.useEffect(() => {
            _ && U();
          }, [_, U]);
          let { onOpenChange: W, triggerPointerDownPosRef: q } = k;
          (n.useEffect(() => {
            if (E) {
              let e = { x: 0, y: 0 },
                t = (t) => {
                  var r, n, i, o;
                  e = {
                    x: Math.abs(
                      Math.round(t.pageX) -
                        (null != (i = null == (r = q.current) ? void 0 : r.x)
                          ? i
                          : 0)
                    ),
                    y: Math.abs(
                      Math.round(t.pageY) -
                        (null != (o = null == (n = q.current) ? void 0 : n.y)
                          ? o
                          : 0)
                    ),
                  };
                },
                r = (r) => {
                  (e.x <= 10 && e.y <= 10
                    ? r.preventDefault()
                    : E.contains(r.target) || W(!1),
                    document.removeEventListener('pointermove', t),
                    (q.current = null));
                };
              return (
                null !== q.current &&
                  (document.addEventListener('pointermove', t),
                  document.addEventListener('pointerup', r, {
                    capture: !0,
                    once: !0,
                  })),
                () => {
                  (document.removeEventListener('pointermove', t),
                    document.removeEventListener('pointerup', r, {
                      capture: !0,
                    }));
                }
              );
            }
          }, [E, W, q]),
            n.useEffect(() => {
              let e = () => W(!1);
              return (
                window.addEventListener('blur', e),
                window.addEventListener('resize', e),
                () => {
                  (window.removeEventListener('blur', e),
                    window.removeEventListener('resize', e));
                }
              );
            }, [W]));
          let [Z, X] = eM((e) => {
              let t = H().filter((e) => !e.disabled),
                r = t.find((e) => e.ref.current === document.activeElement),
                n = ej(t, e, r);
              n && setTimeout(() => n.ref.current.focus());
            }),
            $ = n.useCallback(
              (e, t, r) => {
                let n = !F.current && !r;
                ((void 0 !== k.value && k.value === t) || n) &&
                  (O(e), n && (F.current = !0));
              },
              [k.value]
            ),
            et = n.useCallback(() => (null == E ? void 0 : E.focus()), [E]),
            er = n.useCallback(
              (e, t, r) => {
                let n = !F.current && !r;
                ((void 0 !== k.value && k.value === t) || n) && G(e);
              },
              [k.value]
            ),
            en = 'popper' === i ? ee : J,
            ei =
              en === ee
                ? {
                    side: c,
                    sideOffset: f,
                    align: v,
                    alignOffset: m,
                    arrowPadding: y,
                    collisionBoundary: g,
                    collisionPadding: w,
                    sticky: x,
                    hideWhenDetached: b,
                    avoidCollisions: C,
                  }
                : {};
          return (0, j.jsx)(Q, {
            scope: r,
            content: E,
            viewport: T,
            onViewportChange: I,
            itemRefCallback: $,
            selectedItem: D,
            onItemLeave: et,
            itemTextRefCallback: er,
            focusSelectedItem: U,
            selectedItemText: L,
            position: i,
            isPositioned: _,
            searchRef: Z,
            children: (0, j.jsx)(M.A, {
              as: Y,
              allowPinchZoom: !0,
              children: (0, j.jsx)(p.n, {
                asChild: !0,
                trapped: k.open,
                onMountAutoFocus: (e) => {
                  e.preventDefault();
                },
                onUnmountAutoFocus: (0, s.mK)(o, (e) => {
                  var t;
                  (null == (t = k.trigger) || t.focus({ preventScroll: !0 }),
                    e.preventDefault());
                }),
                children: (0, j.jsx)(d.qW, {
                  asChild: !0,
                  disableOutsidePointerEvents: !0,
                  onEscapeKeyDown: a,
                  onPointerDownOutside: u,
                  onFocusOutside: (e) => e.preventDefault(),
                  onDismiss: () => k.onOpenChange(!1),
                  children: (0, j.jsx)(en, {
                    role: 'listbox',
                    id: k.contentId,
                    'data-state': k.open ? 'open' : 'closed',
                    dir: k.dir,
                    onContextMenu: (e) => e.preventDefault(),
                    ...S,
                    ...ei,
                    onPlaced: () => B(!0),
                    ref: K,
                    style: {
                      display: 'flex',
                      flexDirection: 'column',
                      outline: 'none',
                      ...S.style,
                    },
                    onKeyDown: (0, s.mK)(S.onKeyDown, (e) => {
                      let t = e.ctrlKey || e.altKey || e.metaKey;
                      if (
                        ('Tab' === e.key && e.preventDefault(),
                        t || 1 !== e.key.length || X(e.key),
                        ['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(e.key))
                      ) {
                        let t = H()
                          .filter((e) => !e.disabled)
                          .map((e) => e.ref.current);
                        if (
                          (['ArrowUp', 'End'].includes(e.key) &&
                            (t = t.slice().reverse()),
                          ['ArrowUp', 'ArrowDown'].includes(e.key))
                        ) {
                          let r = e.target,
                            n = t.indexOf(r);
                          t = t.slice(n + 1);
                        }
                        (setTimeout(() => V(t)), e.preventDefault());
                      }
                    }),
                  }),
                }),
              }),
            }),
          });
        });
      $.displayName = 'SelectContentImpl';
      var J = n.forwardRef((e, t) => {
        let { __scopeSelect: r, onPlaced: i, ...s } = e,
          a = A(z, r),
          u = X(z, r),
          [c, d] = n.useState(null),
          [h, p] = n.useState(null),
          f = (0, l.s)(t, (e) => p(e)),
          v = N(r),
          m = n.useRef(!1),
          g = n.useRef(!0),
          {
            viewport: w,
            selectedItem: x,
            selectedItemText: C,
            focusSelectedItem: S,
          } = u,
          R = n.useCallback(() => {
            if (a.trigger && a.valueNode && c && h && w && x && C) {
              let e = a.trigger.getBoundingClientRect(),
                t = h.getBoundingClientRect(),
                r = a.valueNode.getBoundingClientRect(),
                n = C.getBoundingClientRect();
              if ('rtl' !== a.dir) {
                let i = n.left - t.left,
                  s = r.left - i,
                  a = e.left - s,
                  l = e.width + a,
                  u = Math.max(l, t.width),
                  d = window.innerWidth - 10,
                  h = (0, o.q)(s, [10, Math.max(10, d - u)]);
                ((c.style.minWidth = l + 'px'), (c.style.left = h + 'px'));
              } else {
                let i = t.right - n.right,
                  s = window.innerWidth - r.right - i,
                  a = window.innerWidth - e.right - s,
                  l = e.width + a,
                  u = Math.max(l, t.width),
                  d = window.innerWidth - 10,
                  h = (0, o.q)(s, [10, Math.max(10, d - u)]);
                ((c.style.minWidth = l + 'px'), (c.style.right = h + 'px'));
              }
              let s = v(),
                l = window.innerHeight - 20,
                u = w.scrollHeight,
                d = window.getComputedStyle(h),
                p = parseInt(d.borderTopWidth, 10),
                f = parseInt(d.paddingTop, 10),
                y = parseInt(d.borderBottomWidth, 10),
                g = p + f + u + parseInt(d.paddingBottom, 10) + y,
                b = Math.min(5 * x.offsetHeight, g),
                S = window.getComputedStyle(w),
                R = parseInt(S.paddingTop, 10),
                M = parseInt(S.paddingBottom, 10),
                j = e.top + e.height / 2 - 10,
                k = x.offsetHeight / 2,
                E = p + f + (x.offsetTop + k);
              if (E <= j) {
                let e = s.length > 0 && x === s[s.length - 1].ref.current;
                c.style.bottom = '0px';
                let t = Math.max(
                  l - j,
                  k +
                    (e ? M : 0) +
                    (h.clientHeight - w.offsetTop - w.offsetHeight) +
                    y
                );
                c.style.height = E + t + 'px';
              } else {
                let e = s.length > 0 && x === s[0].ref.current;
                c.style.top = '0px';
                let t = Math.max(j, p + w.offsetTop + (e ? R : 0) + k);
                ((c.style.height = t + (g - E) + 'px'),
                  (w.scrollTop = E - j + w.offsetTop));
              }
              ((c.style.margin = ''.concat(10, 'px 0')),
                (c.style.minHeight = b + 'px'),
                (c.style.maxHeight = l + 'px'),
                null == i || i(),
                requestAnimationFrame(() => (m.current = !0)));
            }
          }, [v, a.trigger, a.valueNode, c, h, w, x, C, a.dir, i]);
        (0, b.N)(() => R(), [R]);
        let [M, k] = n.useState();
        (0, b.N)(() => {
          h && k(window.getComputedStyle(h).zIndex);
        }, [h]);
        let E = n.useCallback(
          (e) => {
            e && !0 === g.current && (R(), null == S || S(), (g.current = !1));
          },
          [R, S]
        );
        return (0, j.jsx)(et, {
          scope: r,
          contentWrapper: c,
          shouldExpandOnScrollRef: m,
          onScrollButtonChange: E,
          children: (0, j.jsx)('div', {
            ref: d,
            style: {
              display: 'flex',
              flexDirection: 'column',
              position: 'fixed',
              zIndex: M,
            },
            children: (0, j.jsx)(y.sG.div, {
              ...s,
              ref: f,
              style: { boxSizing: 'border-box', maxHeight: '100%', ...s.style },
            }),
          }),
        });
      });
      J.displayName = 'SelectItemAlignedPosition';
      var ee = n.forwardRef((e, t) => {
        let {
            __scopeSelect: r,
            align: n = 'start',
            collisionPadding: i = 10,
            ...o
          } = e,
          s = O(r);
        return (0, j.jsx)(v.UC, {
          ...s,
          ...o,
          ref: t,
          align: n,
          collisionPadding: i,
          style: {
            boxSizing: 'border-box',
            ...o.style,
            '--radix-select-content-transform-origin':
              'var(--radix-popper-transform-origin)',
            '--radix-select-content-available-width':
              'var(--radix-popper-available-width)',
            '--radix-select-content-available-height':
              'var(--radix-popper-available-height)',
            '--radix-select-trigger-width': 'var(--radix-popper-anchor-width)',
            '--radix-select-trigger-height':
              'var(--radix-popper-anchor-height)',
          },
        });
      });
      ee.displayName = 'SelectPopperPosition';
      var [et, er] = K(z, {}),
        en = 'SelectViewport',
        ei = n.forwardRef((e, t) => {
          let { __scopeSelect: r, nonce: i, ...o } = e,
            a = X(en, r),
            u = er(en, r),
            c = (0, l.s)(t, a.onViewportChange),
            d = n.useRef(0);
          return (0, j.jsxs)(j.Fragment, {
            children: [
              (0, j.jsx)('style', {
                dangerouslySetInnerHTML: {
                  __html:
                    '[data-radix-select-viewport]{scrollbar-width:none;-ms-overflow-style:none;-webkit-overflow-scrolling:touch;}[data-radix-select-viewport]::-webkit-scrollbar{display:none}',
                },
                nonce: i,
              }),
              (0, j.jsx)(T.Slot, {
                scope: r,
                children: (0, j.jsx)(y.sG.div, {
                  'data-radix-select-viewport': '',
                  role: 'presentation',
                  ...o,
                  ref: c,
                  style: {
                    position: 'relative',
                    flex: 1,
                    overflow: 'hidden auto',
                    ...o.style,
                  },
                  onScroll: (0, s.mK)(o.onScroll, (e) => {
                    let t = e.currentTarget,
                      { contentWrapper: r, shouldExpandOnScrollRef: n } = u;
                    if ((null == n ? void 0 : n.current) && r) {
                      let e = Math.abs(d.current - t.scrollTop);
                      if (e > 0) {
                        let n = window.innerHeight - 20,
                          i = Math.max(
                            parseFloat(r.style.minHeight),
                            parseFloat(r.style.height)
                          );
                        if (i < n) {
                          let o = i + e,
                            s = Math.min(n, o),
                            a = o - s;
                          ((r.style.height = s + 'px'),
                            '0px' === r.style.bottom &&
                              ((t.scrollTop = a > 0 ? a : 0),
                              (r.style.justifyContent = 'flex-end')));
                        }
                      }
                    }
                    d.current = t.scrollTop;
                  }),
                }),
              }),
            ],
          });
        });
      ei.displayName = en;
      var eo = 'SelectGroup',
        [es, ea] = K(eo);
      n.forwardRef((e, t) => {
        let { __scopeSelect: r, ...n } = e,
          i = (0, f.B)();
        return (0, j.jsx)(es, {
          scope: r,
          id: i,
          children: (0, j.jsx)(y.sG.div, {
            role: 'group',
            'aria-labelledby': i,
            ...n,
            ref: t,
          }),
        });
      }).displayName = eo;
      var el = 'SelectLabel';
      n.forwardRef((e, t) => {
        let { __scopeSelect: r, ...n } = e,
          i = ea(el, r);
        return (0, j.jsx)(y.sG.div, { id: i.id, ...n, ref: t });
      }).displayName = el;
      var eu = 'SelectItem',
        [ec, ed] = K(eu),
        eh = n.forwardRef((e, t) => {
          let {
              __scopeSelect: r,
              value: i,
              disabled: o = !1,
              textValue: a,
              ...u
            } = e,
            c = A(eu, r),
            d = X(eu, r),
            h = c.value === i,
            [p, v] = n.useState(null != a ? a : ''),
            [m, g] = n.useState(!1),
            w = (0, l.s)(t, (e) => {
              var t;
              return null == (t = d.itemRefCallback)
                ? void 0
                : t.call(d, e, i, o);
            }),
            x = (0, f.B)(),
            b = n.useRef('touch'),
            C = () => {
              o || (c.onValueChange(i), c.onOpenChange(!1));
            };
          if ('' === i)
            throw Error(
              'A <Select.Item /> must have a value prop that is not an empty string. This is because the Select value can be set to an empty string to clear the selection and show the placeholder.'
            );
          return (0, j.jsx)(ec, {
            scope: r,
            value: i,
            disabled: o,
            textId: x,
            isSelected: h,
            onItemTextChange: n.useCallback((e) => {
              v((t) => {
                var r;
                return (
                  t ||
                  (null != (r = null == e ? void 0 : e.textContent)
                    ? r
                    : ''
                  ).trim()
                );
              });
            }, []),
            children: (0, j.jsx)(T.ItemSlot, {
              scope: r,
              value: i,
              disabled: o,
              textValue: p,
              children: (0, j.jsx)(y.sG.div, {
                role: 'option',
                'aria-labelledby': x,
                'data-highlighted': m ? '' : void 0,
                'aria-selected': h && m,
                'data-state': h ? 'checked' : 'unchecked',
                'aria-disabled': o || void 0,
                'data-disabled': o ? '' : void 0,
                tabIndex: o ? void 0 : -1,
                ...u,
                ref: w,
                onFocus: (0, s.mK)(u.onFocus, () => g(!0)),
                onBlur: (0, s.mK)(u.onBlur, () => g(!1)),
                onClick: (0, s.mK)(u.onClick, () => {
                  'mouse' !== b.current && C();
                }),
                onPointerUp: (0, s.mK)(u.onPointerUp, () => {
                  'mouse' === b.current && C();
                }),
                onPointerDown: (0, s.mK)(u.onPointerDown, (e) => {
                  b.current = e.pointerType;
                }),
                onPointerMove: (0, s.mK)(u.onPointerMove, (e) => {
                  if (((b.current = e.pointerType), o)) {
                    var t;
                    null == (t = d.onItemLeave) || t.call(d);
                  } else
                    'mouse' === b.current &&
                      e.currentTarget.focus({ preventScroll: !0 });
                }),
                onPointerLeave: (0, s.mK)(u.onPointerLeave, (e) => {
                  if (e.currentTarget === document.activeElement) {
                    var t;
                    null == (t = d.onItemLeave) || t.call(d);
                  }
                }),
                onKeyDown: (0, s.mK)(u.onKeyDown, (e) => {
                  var t;
                  ((null == (t = d.searchRef) ? void 0 : t.current) === '' ||
                    ' ' !== e.key) &&
                    (E.includes(e.key) && C(),
                    ' ' === e.key && e.preventDefault());
                }),
              }),
            }),
          });
        });
      eh.displayName = eu;
      var ep = 'SelectItemText',
        ef = n.forwardRef((e, t) => {
          let { __scopeSelect: r, className: o, style: s, ...a } = e,
            u = A(ep, r),
            c = X(ep, r),
            d = ed(ep, r),
            h = H(ep, r),
            [p, f] = n.useState(null),
            v = (0, l.s)(
              t,
              (e) => f(e),
              d.onItemTextChange,
              (e) => {
                var t;
                return null == (t = c.itemTextRefCallback)
                  ? void 0
                  : t.call(c, e, d.value, d.disabled);
              }
            ),
            m = null == p ? void 0 : p.textContent,
            g = n.useMemo(
              () =>
                (0, j.jsx)(
                  'option',
                  { value: d.value, disabled: d.disabled, children: m },
                  d.value
                ),
              [d.disabled, d.value, m]
            ),
            { onNativeOptionAdd: w, onNativeOptionRemove: x } = h;
          return (
            (0, b.N)(() => (w(g), () => x(g)), [w, x, g]),
            (0, j.jsxs)(j.Fragment, {
              children: [
                (0, j.jsx)(y.sG.span, { id: d.textId, ...a, ref: v }),
                d.isSelected && u.valueNode && !u.valueNodeHasChildren
                  ? i.createPortal(a.children, u.valueNode)
                  : null,
              ],
            })
          );
        });
      ef.displayName = ep;
      var ev = 'SelectItemIndicator',
        em = n.forwardRef((e, t) => {
          let { __scopeSelect: r, ...n } = e;
          return ed(ev, r).isSelected
            ? (0, j.jsx)(y.sG.span, { 'aria-hidden': !0, ...n, ref: t })
            : null;
        });
      em.displayName = ev;
      var ey = 'SelectScrollUpButton',
        eg = n.forwardRef((e, t) => {
          let r = X(ey, e.__scopeSelect),
            i = er(ey, e.__scopeSelect),
            [o, s] = n.useState(!1),
            a = (0, l.s)(t, i.onScrollButtonChange);
          return (
            (0, b.N)(() => {
              if (r.viewport && r.isPositioned) {
                let e = function () {
                    s(t.scrollTop > 0);
                  },
                  t = r.viewport;
                return (
                  e(),
                  t.addEventListener('scroll', e),
                  () => t.removeEventListener('scroll', e)
                );
              }
            }, [r.viewport, r.isPositioned]),
            o
              ? (0, j.jsx)(eb, {
                  ...e,
                  ref: a,
                  onAutoScroll: () => {
                    let { viewport: e, selectedItem: t } = r;
                    e && t && (e.scrollTop = e.scrollTop - t.offsetHeight);
                  },
                })
              : null
          );
        });
      eg.displayName = ey;
      var ew = 'SelectScrollDownButton',
        ex = n.forwardRef((e, t) => {
          let r = X(ew, e.__scopeSelect),
            i = er(ew, e.__scopeSelect),
            [o, s] = n.useState(!1),
            a = (0, l.s)(t, i.onScrollButtonChange);
          return (
            (0, b.N)(() => {
              if (r.viewport && r.isPositioned) {
                let e = function () {
                    let e = t.scrollHeight - t.clientHeight;
                    s(Math.ceil(t.scrollTop) < e);
                  },
                  t = r.viewport;
                return (
                  e(),
                  t.addEventListener('scroll', e),
                  () => t.removeEventListener('scroll', e)
                );
              }
            }, [r.viewport, r.isPositioned]),
            o
              ? (0, j.jsx)(eb, {
                  ...e,
                  ref: a,
                  onAutoScroll: () => {
                    let { viewport: e, selectedItem: t } = r;
                    e && t && (e.scrollTop = e.scrollTop + t.offsetHeight);
                  },
                })
              : null
          );
        });
      ex.displayName = ew;
      var eb = n.forwardRef((e, t) => {
        let { __scopeSelect: r, onAutoScroll: i, ...o } = e,
          a = X('SelectScrollButton', r),
          l = n.useRef(null),
          u = N(r),
          c = n.useCallback(() => {
            null !== l.current &&
              (window.clearInterval(l.current), (l.current = null));
          }, []);
        return (
          n.useEffect(() => () => c(), [c]),
          (0, b.N)(() => {
            var e;
            let t = u().find((e) => e.ref.current === document.activeElement);
            null == t ||
              null == (e = t.ref.current) ||
              e.scrollIntoView({ block: 'nearest' });
          }, [u]),
          (0, j.jsx)(y.sG.div, {
            'aria-hidden': !0,
            ...o,
            ref: t,
            style: { flexShrink: 0, ...o.style },
            onPointerDown: (0, s.mK)(o.onPointerDown, () => {
              null === l.current && (l.current = window.setInterval(i, 50));
            }),
            onPointerMove: (0, s.mK)(o.onPointerMove, () => {
              var e;
              (null == (e = a.onItemLeave) || e.call(a),
                null === l.current && (l.current = window.setInterval(i, 50)));
            }),
            onPointerLeave: (0, s.mK)(o.onPointerLeave, () => {
              c();
            }),
          })
        );
      });
      n.forwardRef((e, t) => {
        let { __scopeSelect: r, ...n } = e;
        return (0, j.jsx)(y.sG.div, { 'aria-hidden': !0, ...n, ref: t });
      }).displayName = 'SelectSeparator';
      var eC = 'SelectArrow';
      n.forwardRef((e, t) => {
        let { __scopeSelect: r, ...n } = e,
          i = O(r),
          o = A(eC, r),
          s = X(eC, r);
        return o.open && 'popper' === s.position
          ? (0, j.jsx)(v.i3, { ...i, ...n, ref: t })
          : null;
      }).displayName = eC;
      var eS = n.forwardRef((e, t) => {
        let { __scopeSelect: r, value: i, ...o } = e,
          s = n.useRef(null),
          a = (0, l.s)(t, s),
          u = (0, C.Z)(i);
        return (
          n.useEffect(() => {
            let e = s.current;
            if (!e) return;
            let t = Object.getOwnPropertyDescriptor(
              window.HTMLSelectElement.prototype,
              'value'
            ).set;
            if (u !== i && t) {
              let r = new Event('change', { bubbles: !0 });
              (t.call(e, i), e.dispatchEvent(r));
            }
          }, [u, i]),
          (0, j.jsx)(y.sG.select, {
            ...o,
            style: { ...S.Qg, ...o.style },
            ref: a,
            defaultValue: i,
          })
        );
      });
      function eR(e) {
        return '' === e || void 0 === e;
      }
      function eM(e) {
        let t = (0, w.c)(e),
          r = n.useRef(''),
          i = n.useRef(0),
          o = n.useCallback(
            (e) => {
              let n = r.current + e;
              (t(n),
                (function e(t) {
                  ((r.current = t),
                    window.clearTimeout(i.current),
                    '' !== t &&
                      (i.current = window.setTimeout(() => e(''), 1e3)));
                })(n));
            },
            [t]
          ),
          s = n.useCallback(() => {
            ((r.current = ''), window.clearTimeout(i.current));
          }, []);
        return (
          n.useEffect(() => () => window.clearTimeout(i.current), []),
          [r, o, s]
        );
      }
      function ej(e, t, r) {
        var n, i;
        let o =
            t.length > 1 && Array.from(t).every((e) => e === t[0]) ? t[0] : t,
          s = r ? e.indexOf(r) : -1,
          a =
            ((n = e),
            (i = Math.max(s, 0)),
            n.map((e, t) => n[(i + t) % n.length]));
        1 === o.length && (a = a.filter((e) => e !== r));
        let l = a.find((e) =>
          e.textValue.toLowerCase().startsWith(o.toLowerCase())
        );
        return l !== r ? l : void 0;
      }
      eS.displayName = 'SelectBubbleInput';
      var ek = _,
        eE = F,
        eP = U,
        eT = W,
        eN = q,
        eI = Z,
        eK = ei,
        eD = eh,
        eO = ef,
        eL = em,
        eA = eg,
        eG = ex;
    },
    4967: (e, t, r) => {
      r.d(t, { A: () => n });
      let n = (0, r(3929).A)('chevron-up', [
        ['path', { d: 'm18 15-6-6-6 6', key: '153udz' }],
      ]);
    },
    6013: (e, t, r) => {
      r.d(t, { n: () => c });
      var n = r(4398),
        i = r(1331),
        o = r(5688),
        s = r(3145),
        a = r(9205),
        l = class extends s.Q {
          #e;
          #o = void 0;
          #s;
          #a;
          constructor(e, t) {
            (super(),
              (this.#e = e),
              this.setOptions(t),
              this.bindMethods(),
              this.#l());
          }
          bindMethods() {
            ((this.mutate = this.mutate.bind(this)),
              (this.reset = this.reset.bind(this)));
          }
          setOptions(e) {
            let t = this.options;
            ((this.options = this.#e.defaultMutationOptions(e)),
              (0, a.f8)(this.options, t) ||
                this.#e
                  .getMutationCache()
                  .notify({
                    type: 'observerOptionsUpdated',
                    mutation: this.#s,
                    observer: this,
                  }),
              t?.mutationKey &&
              this.options.mutationKey &&
              (0, a.EN)(t.mutationKey) !== (0, a.EN)(this.options.mutationKey)
                ? this.reset()
                : this.#s?.state.status === 'pending' &&
                  this.#s.setOptions(this.options));
          }
          onUnsubscribe() {
            this.hasListeners() || this.#s?.removeObserver(this);
          }
          onMutationUpdate(e) {
            (this.#l(), this.#u(e));
          }
          getCurrentResult() {
            return this.#o;
          }
          reset() {
            (this.#s?.removeObserver(this),
              (this.#s = void 0),
              this.#l(),
              this.#u());
          }
          mutate(e, t) {
            return (
              (this.#a = t),
              this.#s?.removeObserver(this),
              (this.#s = this.#e
                .getMutationCache()
                .build(this.#e, this.options)),
              this.#s.addObserver(this),
              this.#s.execute(e)
            );
          }
          #l() {
            let e = this.#s?.state ?? (0, i.$)();
            this.#o = {
              ...e,
              isPending: 'pending' === e.status,
              isSuccess: 'success' === e.status,
              isError: 'error' === e.status,
              isIdle: 'idle' === e.status,
              mutate: this.mutate,
              reset: this.reset,
            };
          }
          #u(e) {
            o.jG.batch(() => {
              if (this.#a && this.hasListeners()) {
                let t = this.#o.variables,
                  r = this.#o.context,
                  n = {
                    client: this.#e,
                    meta: this.options.meta,
                    mutationKey: this.options.mutationKey,
                  };
                e?.type === 'success'
                  ? (this.#a.onSuccess?.(e.data, t, r, n),
                    this.#a.onSettled?.(e.data, null, t, r, n))
                  : e?.type === 'error' &&
                    (this.#a.onError?.(e.error, t, r, n),
                    this.#a.onSettled?.(void 0, e.error, t, r, n));
              }
              this.listeners.forEach((e) => {
                e(this.#o);
              });
            });
          }
        },
        u = r(100);
      function c(e, t) {
        let r = (0, u.jE)(t),
          [i] = n.useState(() => new l(r, e));
        n.useEffect(() => {
          i.setOptions(e);
        }, [i, e]);
        let s = n.useSyncExternalStore(
            n.useCallback((e) => i.subscribe(o.jG.batchCalls(e)), [i]),
            () => i.getCurrentResult(),
            () => i.getCurrentResult()
          ),
          c = n.useCallback(
            (e, t) => {
              i.mutate(e, t).catch(a.lQ);
            },
            [i]
          );
        if (s.error && (0, a.GU)(i.options.throwOnError, [s.error]))
          throw s.error;
        return { ...s, mutate: c, mutateAsync: s.mutate };
      }
    },
    6017: (e, t, r) => {
      r.d(t, { Z: () => i });
      var n = r(4398);
      function i(e) {
        let t = n.useRef({ value: e, previous: e });
        return n.useMemo(
          () => (
            t.current.value !== e &&
              ((t.current.previous = t.current.value), (t.current.value = e)),
            t.current.previous
          ),
          [e]
        );
      }
    },
    8940: (e, t, r) => {
      r.d(t, { A: () => n });
      let n = (0, r(3929).A)('check', [
        ['path', { d: 'M20 6 9 17l-5-5', key: '1gmf2c' }],
      ]);
    },
    9994: (e, t, r) => {
      r.d(t, { A: () => n });
      let n = (0, r(3929).A)('chevron-down', [
        ['path', { d: 'm6 9 6 6 6-6', key: 'qrunsl' }],
      ]);
    },
  },
]);
