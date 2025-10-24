'use strict';
(self.webpackChunk_N_E = self.webpackChunk_N_E || []).push([
  [29],
  {
    100: (t, e, i) => {
      i.d(e, { Ht: () => a, jE: () => o });
      var s = i(4398),
        r = i(3422),
        n = s.createContext(void 0),
        o = (t) => {
          let e = s.useContext(n);
          if (t) return t;
          if (!e)
            throw Error(
              'No QueryClient set, use QueryClientProvider to set one'
            );
          return e;
        },
        a = (t) => {
          let { client: e, children: i } = t;
          return (
            s.useEffect(
              () => (
                e.mount(),
                () => {
                  e.unmount();
                }
              ),
              [e]
            ),
            (0, r.jsx)(n.Provider, { value: e, children: i })
          );
        };
    },
    449: (t, e, i) => {
      i.d(e, { k: () => n });
      var s = i(8890),
        r = i(9205),
        n = class {
          #t;
          destroy() {
            this.clearGcTimeout();
          }
          scheduleGc() {
            (this.clearGcTimeout(),
              (0, r.gn)(this.gcTime) &&
                (this.#t = s.zs.setTimeout(() => {
                  this.optionalRemove();
                }, this.gcTime)));
          }
          updateGcTime(t) {
            this.gcTime = Math.max(this.gcTime || 0, t ?? (r.S$ ? 1 / 0 : 3e5));
          }
          clearGcTimeout() {
            this.#t && (s.zs.clearTimeout(this.#t), (this.#t = void 0));
          }
        };
    },
    2925: (t, e, i) => {
      i.d(e, { II: () => h, cc: () => c, v_: () => u });
      var s = i(6479),
        r = i(7478),
        n = i(7623),
        o = i(9205);
      function a(t) {
        return Math.min(1e3 * 2 ** t, 3e4);
      }
      function u(t) {
        return (t ?? 'online') !== 'online' || r.t.isOnline();
      }
      var c = class extends Error {
        constructor(t) {
          (super('CancelledError'),
            (this.revert = t?.revert),
            (this.silent = t?.silent));
        }
      };
      function h(t) {
        let e,
          i = !1,
          h = 0,
          l = (0, n.T)(),
          d = () => 'pending' !== l.status,
          f = () =>
            s.m.isFocused() &&
            ('always' === t.networkMode || r.t.isOnline()) &&
            t.canRun(),
          p = () => u(t.networkMode) && t.canRun(),
          y = (t) => {
            d() || (e?.(), l.resolve(t));
          },
          v = (t) => {
            d() || (e?.(), l.reject(t));
          },
          b = () =>
            new Promise((i) => {
              ((e = (t) => {
                (d() || f()) && i(t);
              }),
                t.onPause?.());
            }).then(() => {
              ((e = void 0), d() || t.onContinue?.());
            }),
          m = () => {
            let e;
            if (d()) return;
            let s = 0 === h ? t.initialPromise : void 0;
            try {
              e = s ?? t.fn();
            } catch (t) {
              e = Promise.reject(t);
            }
            Promise.resolve(e)
              .then(y)
              .catch((e) => {
                if (d()) return;
                let s = t.retry ?? 3 * !o.S$,
                  r = t.retryDelay ?? a,
                  n = 'function' == typeof r ? r(h, e) : r,
                  u =
                    !0 === s ||
                    ('number' == typeof s && h < s) ||
                    ('function' == typeof s && s(h, e));
                if (i || !u) return void v(e);
                (h++,
                  t.onFail?.(h, e),
                  (0, o.yy)(n)
                    .then(() => (f() ? void 0 : b()))
                    .then(() => {
                      i ? v(e) : m();
                    }));
              });
          };
        return {
          promise: l,
          status: () => l.status,
          cancel: (e) => {
            if (!d()) {
              let i = new c(e);
              (v(i), t.onCancel?.(i));
            }
          },
          continue: () => (e?.(), l),
          cancelRetry: () => {
            i = !0;
          },
          continueRetry: () => {
            i = !1;
          },
          canStart: p,
          start: () => (p() ? m() : b().then(m), l),
        };
      }
    },
    3145: (t, e, i) => {
      i.d(e, { Q: () => s });
      var s = class {
        constructor() {
          ((this.listeners = new Set()),
            (this.subscribe = this.subscribe.bind(this)));
        }
        subscribe(t) {
          return (
            this.listeners.add(t),
            this.onSubscribe(),
            () => {
              (this.listeners.delete(t), this.onUnsubscribe());
            }
          );
        }
        hasListeners() {
          return this.listeners.size > 0;
        }
        onSubscribe() {}
        onUnsubscribe() {}
      };
    },
    5688: (t, e, i) => {
      i.d(e, { jG: () => r });
      var s = i(8890).Zq,
        r = (function () {
          let t = [],
            e = 0,
            i = (t) => {
              t();
            },
            r = (t) => {
              t();
            },
            n = s,
            o = (s) => {
              e
                ? t.push(s)
                : n(() => {
                    i(s);
                  });
            },
            a = () => {
              let e = t;
              ((t = []),
                e.length &&
                  n(() => {
                    r(() => {
                      e.forEach((t) => {
                        i(t);
                      });
                    });
                  }));
            };
          return {
            batch: (t) => {
              let i;
              e++;
              try {
                i = t();
              } finally {
                --e || a();
              }
              return i;
            },
            batchCalls:
              (t) =>
              (...e) => {
                o(() => {
                  t(...e);
                });
              },
            schedule: o,
            setNotifyFunction: (t) => {
              i = t;
            },
            setBatchNotifyFunction: (t) => {
              r = t;
            },
            setScheduler: (t) => {
              n = t;
            },
          };
        })();
    },
    6479: (t, e, i) => {
      i.d(e, { m: () => n });
      var s = i(3145),
        r = i(9205),
        n = new (class extends s.Q {
          #e;
          #i;
          #s;
          constructor() {
            (super(),
              (this.#s = (t) => {
                if (!r.S$ && window.addEventListener) {
                  let e = () => t();
                  return (
                    window.addEventListener('visibilitychange', e, !1),
                    () => {
                      window.removeEventListener('visibilitychange', e);
                    }
                  );
                }
              }));
          }
          onSubscribe() {
            this.#i || this.setEventListener(this.#s);
          }
          onUnsubscribe() {
            this.hasListeners() || (this.#i?.(), (this.#i = void 0));
          }
          setEventListener(t) {
            ((this.#s = t),
              this.#i?.(),
              (this.#i = t((t) => {
                'boolean' == typeof t ? this.setFocused(t) : this.onFocus();
              })));
          }
          setFocused(t) {
            this.#e !== t && ((this.#e = t), this.onFocus());
          }
          onFocus() {
            let t = this.isFocused();
            this.listeners.forEach((e) => {
              e(t);
            });
          }
          isFocused() {
            return 'boolean' == typeof this.#e
              ? this.#e
              : globalThis.document?.visibilityState !== 'hidden';
          }
        })();
    },
    7478: (t, e, i) => {
      i.d(e, { t: () => n });
      var s = i(3145),
        r = i(9205),
        n = new (class extends s.Q {
          #r = !0;
          #i;
          #s;
          constructor() {
            (super(),
              (this.#s = (t) => {
                if (!r.S$ && window.addEventListener) {
                  let e = () => t(!0),
                    i = () => t(!1);
                  return (
                    window.addEventListener('online', e, !1),
                    window.addEventListener('offline', i, !1),
                    () => {
                      (window.removeEventListener('online', e),
                        window.removeEventListener('offline', i));
                    }
                  );
                }
              }));
          }
          onSubscribe() {
            this.#i || this.setEventListener(this.#s);
          }
          onUnsubscribe() {
            this.hasListeners() || (this.#i?.(), (this.#i = void 0));
          }
          setEventListener(t) {
            ((this.#s = t),
              this.#i?.(),
              (this.#i = t(this.setOnline.bind(this))));
          }
          setOnline(t) {
            this.#r !== t &&
              ((this.#r = t),
              this.listeners.forEach((e) => {
                e(t);
              }));
          }
          isOnline() {
            return this.#r;
          }
        })();
    },
    7623: (t, e, i) => {
      function s() {
        let t,
          e,
          i = new Promise((i, s) => {
            ((t = i), (e = s));
          });
        function s(t) {
          (Object.assign(i, t), delete i.resolve, delete i.reject);
        }
        return (
          (i.status = 'pending'),
          i.catch(() => {}),
          (i.resolve = (e) => {
            (s({ status: 'fulfilled', value: e }), t(e));
          }),
          (i.reject = (t) => {
            (s({ status: 'rejected', reason: t }), e(t));
          }),
          i
        );
      }
      i.d(e, { T: () => s });
    },
    8192: (t, e, i) => {
      i.d(e, { X: () => a, k: () => u });
      var s = i(9205),
        r = i(5688),
        n = i(2925),
        o = i(449),
        a = class extends o.k {
          #n;
          #o;
          #a;
          #u;
          #c;
          #h;
          #l;
          constructor(t) {
            (super(),
              (this.#l = !1),
              (this.#h = t.defaultOptions),
              this.setOptions(t.options),
              (this.observers = []),
              (this.#u = t.client),
              (this.#a = this.#u.getQueryCache()),
              (this.queryKey = t.queryKey),
              (this.queryHash = t.queryHash),
              (this.#n = c(this.options)),
              (this.state = t.state ?? this.#n),
              this.scheduleGc());
          }
          get meta() {
            return this.options.meta;
          }
          get promise() {
            return this.#c?.promise;
          }
          setOptions(t) {
            if (
              ((this.options = { ...this.#h, ...t }),
              this.updateGcTime(this.options.gcTime),
              this.state && void 0 === this.state.data)
            ) {
              let t = c(this.options);
              void 0 !== t.data &&
                (this.setData(t.data, {
                  updatedAt: t.dataUpdatedAt,
                  manual: !0,
                }),
                (this.#n = t));
            }
          }
          optionalRemove() {
            this.observers.length ||
              'idle' !== this.state.fetchStatus ||
              this.#a.remove(this);
          }
          setData(t, e) {
            let i = (0, s.pl)(this.state.data, t, this.options);
            return (
              this.#d({
                data: i,
                type: 'success',
                dataUpdatedAt: e?.updatedAt,
                manual: e?.manual,
              }),
              i
            );
          }
          setState(t, e) {
            this.#d({ type: 'setState', state: t, setStateOptions: e });
          }
          cancel(t) {
            let e = this.#c?.promise;
            return (
              this.#c?.cancel(t),
              e ? e.then(s.lQ).catch(s.lQ) : Promise.resolve()
            );
          }
          destroy() {
            (super.destroy(), this.cancel({ silent: !0 }));
          }
          reset() {
            (this.destroy(), this.setState(this.#n));
          }
          isActive() {
            return this.observers.some(
              (t) => !1 !== (0, s.Eh)(t.options.enabled, this)
            );
          }
          isDisabled() {
            return this.getObserversCount() > 0
              ? !this.isActive()
              : this.options.queryFn === s.hT ||
                  this.state.dataUpdateCount + this.state.errorUpdateCount ===
                    0;
          }
          isStatic() {
            return (
              this.getObserversCount() > 0 &&
              this.observers.some(
                (t) => 'static' === (0, s.d2)(t.options.staleTime, this)
              )
            );
          }
          isStale() {
            return this.getObserversCount() > 0
              ? this.observers.some((t) => t.getCurrentResult().isStale)
              : void 0 === this.state.data || this.state.isInvalidated;
          }
          isStaleByTime(t = 0) {
            return (
              void 0 === this.state.data ||
              ('static' !== t &&
                (!!this.state.isInvalidated ||
                  !(0, s.j3)(this.state.dataUpdatedAt, t)))
            );
          }
          onFocus() {
            let t = this.observers.find((t) => t.shouldFetchOnWindowFocus());
            (t?.refetch({ cancelRefetch: !1 }), this.#c?.continue());
          }
          onOnline() {
            let t = this.observers.find((t) => t.shouldFetchOnReconnect());
            (t?.refetch({ cancelRefetch: !1 }), this.#c?.continue());
          }
          addObserver(t) {
            this.observers.includes(t) ||
              (this.observers.push(t),
              this.clearGcTimeout(),
              this.#a.notify({
                type: 'observerAdded',
                query: this,
                observer: t,
              }));
          }
          removeObserver(t) {
            this.observers.includes(t) &&
              ((this.observers = this.observers.filter((e) => e !== t)),
              this.observers.length ||
                (this.#c &&
                  (this.#l
                    ? this.#c.cancel({ revert: !0 })
                    : this.#c.cancelRetry()),
                this.scheduleGc()),
              this.#a.notify({
                type: 'observerRemoved',
                query: this,
                observer: t,
              }));
          }
          getObserversCount() {
            return this.observers.length;
          }
          invalidate() {
            this.state.isInvalidated || this.#d({ type: 'invalidate' });
          }
          async fetch(t, e) {
            if (
              'idle' !== this.state.fetchStatus &&
              this.#c?.status() !== 'rejected'
            ) {
              if (void 0 !== this.state.data && e?.cancelRefetch)
                this.cancel({ silent: !0 });
              else if (this.#c)
                return (this.#c.continueRetry(), this.#c.promise);
            }
            if ((t && this.setOptions(t), !this.options.queryFn)) {
              let t = this.observers.find((t) => t.options.queryFn);
              t && this.setOptions(t.options);
            }
            let i = new AbortController(),
              r = (t) => {
                Object.defineProperty(t, 'signal', {
                  enumerable: !0,
                  get: () => ((this.#l = !0), i.signal),
                });
              },
              o = () => {
                let t = (0, s.ZM)(this.options, e),
                  i = (() => {
                    let t = {
                      client: this.#u,
                      queryKey: this.queryKey,
                      meta: this.meta,
                    };
                    return (r(t), t);
                  })();
                return ((this.#l = !1), this.options.persister)
                  ? this.options.persister(t, i, this)
                  : t(i);
              },
              a = (() => {
                let t = {
                  fetchOptions: e,
                  options: this.options,
                  queryKey: this.queryKey,
                  client: this.#u,
                  state: this.state,
                  fetchFn: o,
                };
                return (r(t), t);
              })();
            (this.options.behavior?.onFetch(a, this),
              (this.#o = this.state),
              ('idle' === this.state.fetchStatus ||
                this.state.fetchMeta !== a.fetchOptions?.meta) &&
                this.#d({ type: 'fetch', meta: a.fetchOptions?.meta }),
              (this.#c = (0, n.II)({
                initialPromise: e?.initialPromise,
                fn: a.fetchFn,
                onCancel: (t) => {
                  (t instanceof n.cc &&
                    t.revert &&
                    this.setState({ ...this.#o, fetchStatus: 'idle' }),
                    i.abort());
                },
                onFail: (t, e) => {
                  this.#d({ type: 'failed', failureCount: t, error: e });
                },
                onPause: () => {
                  this.#d({ type: 'pause' });
                },
                onContinue: () => {
                  this.#d({ type: 'continue' });
                },
                retry: a.options.retry,
                retryDelay: a.options.retryDelay,
                networkMode: a.options.networkMode,
                canRun: () => !0,
              })));
            try {
              let t = await this.#c.start();
              if (void 0 === t)
                throw Error(`${this.queryHash} data is undefined`);
              return (
                this.setData(t),
                this.#a.config.onSuccess?.(t, this),
                this.#a.config.onSettled?.(t, this.state.error, this),
                t
              );
            } catch (t) {
              if (t instanceof n.cc) {
                if (t.silent) return this.#c.promise;
                else if (t.revert) {
                  if (void 0 === this.state.data) throw t;
                  return this.state.data;
                }
              }
              throw (
                this.#d({ type: 'error', error: t }),
                this.#a.config.onError?.(t, this),
                this.#a.config.onSettled?.(this.state.data, t, this),
                t
              );
            } finally {
              this.scheduleGc();
            }
          }
          #d(t) {
            ((this.state = ((e) => {
              switch (t.type) {
                case 'failed':
                  return {
                    ...e,
                    fetchFailureCount: t.failureCount,
                    fetchFailureReason: t.error,
                  };
                case 'pause':
                  return { ...e, fetchStatus: 'paused' };
                case 'continue':
                  return { ...e, fetchStatus: 'fetching' };
                case 'fetch':
                  return {
                    ...e,
                    ...u(e.data, this.options),
                    fetchMeta: t.meta ?? null,
                  };
                case 'success':
                  let i = {
                    ...e,
                    data: t.data,
                    dataUpdateCount: e.dataUpdateCount + 1,
                    dataUpdatedAt: t.dataUpdatedAt ?? Date.now(),
                    error: null,
                    isInvalidated: !1,
                    status: 'success',
                    ...(!t.manual && {
                      fetchStatus: 'idle',
                      fetchFailureCount: 0,
                      fetchFailureReason: null,
                    }),
                  };
                  return ((this.#o = t.manual ? i : void 0), i);
                case 'error':
                  let s = t.error;
                  return {
                    ...e,
                    error: s,
                    errorUpdateCount: e.errorUpdateCount + 1,
                    errorUpdatedAt: Date.now(),
                    fetchFailureCount: e.fetchFailureCount + 1,
                    fetchFailureReason: s,
                    fetchStatus: 'idle',
                    status: 'error',
                  };
                case 'invalidate':
                  return { ...e, isInvalidated: !0 };
                case 'setState':
                  return { ...e, ...t.state };
              }
            })(this.state)),
              r.jG.batch(() => {
                (this.observers.forEach((t) => {
                  t.onQueryUpdate();
                }),
                  this.#a.notify({ query: this, type: 'updated', action: t }));
              }));
          }
        };
      function u(t, e) {
        return {
          fetchFailureCount: 0,
          fetchFailureReason: null,
          fetchStatus: (0, n.v_)(e.networkMode) ? 'fetching' : 'paused',
          ...(void 0 === t && { error: null, status: 'pending' }),
        };
      }
      function c(t) {
        let e =
            'function' == typeof t.initialData
              ? t.initialData()
              : t.initialData,
          i = void 0 !== e,
          s = i
            ? 'function' == typeof t.initialDataUpdatedAt
              ? t.initialDataUpdatedAt()
              : t.initialDataUpdatedAt
            : 0;
        return {
          data: e,
          dataUpdateCount: 0,
          dataUpdatedAt: i ? (s ?? Date.now()) : 0,
          error: null,
          errorUpdateCount: 0,
          errorUpdatedAt: 0,
          fetchFailureCount: 0,
          fetchFailureReason: null,
          fetchMeta: null,
          isInvalidated: !1,
          status: i ? 'success' : 'pending',
          fetchStatus: 'idle',
        };
      }
    },
    8890: (t, e, i) => {
      i.d(e, { Zq: () => n, zs: () => r });
      var s = {
          setTimeout: (t, e) => setTimeout(t, e),
          clearTimeout: (t) => clearTimeout(t),
          setInterval: (t, e) => setInterval(t, e),
          clearInterval: (t) => clearInterval(t),
        },
        r = new (class {
          #f = s;
          #p = !1;
          setTimeoutProvider(t) {
            this.#f = t;
          }
          setTimeout(t, e) {
            return this.#f.setTimeout(t, e);
          }
          clearTimeout(t) {
            this.#f.clearTimeout(t);
          }
          setInterval(t, e) {
            return this.#f.setInterval(t, e);
          }
          clearInterval(t) {
            this.#f.clearInterval(t);
          }
        })();
      function n(t) {
        setTimeout(t, 0);
      }
    },
    9205: (t, e, i) => {
      i.d(e, {
        Cp: () => y,
        EN: () => p,
        Eh: () => h,
        F$: () => f,
        GU: () => E,
        MK: () => l,
        S$: () => r,
        ZM: () => j,
        ZZ: () => F,
        Zw: () => o,
        d2: () => c,
        f8: () => b,
        gn: () => a,
        hT: () => T,
        j3: () => u,
        lQ: () => n,
        nJ: () => d,
        pl: () => O,
        y9: () => C,
        yy: () => w,
      });
      var s = i(8890),
        r = 'undefined' == typeof window || 'Deno' in globalThis;
      function n() {}
      function o(t, e) {
        return 'function' == typeof t ? t(e) : t;
      }
      function a(t) {
        return 'number' == typeof t && t >= 0 && t !== 1 / 0;
      }
      function u(t, e) {
        return Math.max(t + (e || 0) - Date.now(), 0);
      }
      function c(t, e) {
        return 'function' == typeof t ? t(e) : t;
      }
      function h(t, e) {
        return 'function' == typeof t ? t(e) : t;
      }
      function l(t, e) {
        let {
          type: i = 'all',
          exact: s,
          fetchStatus: r,
          predicate: n,
          queryKey: o,
          stale: a,
        } = t;
        if (o) {
          if (s) {
            if (e.queryHash !== f(o, e.options)) return !1;
          } else if (!y(e.queryKey, o)) return !1;
        }
        if ('all' !== i) {
          let t = e.isActive();
          if (('active' === i && !t) || ('inactive' === i && t)) return !1;
        }
        return (
          ('boolean' != typeof a || e.isStale() === a) &&
          (!r || r === e.state.fetchStatus) &&
          (!n || !!n(e))
        );
      }
      function d(t, e) {
        let { exact: i, status: s, predicate: r, mutationKey: n } = t;
        if (n) {
          if (!e.options.mutationKey) return !1;
          if (i) {
            if (p(e.options.mutationKey) !== p(n)) return !1;
          } else if (!y(e.options.mutationKey, n)) return !1;
        }
        return (!s || e.state.status === s) && (!r || !!r(e));
      }
      function f(t, e) {
        return (e?.queryKeyHashFn || p)(t);
      }
      function p(t) {
        return JSON.stringify(t, (t, e) =>
          g(e)
            ? Object.keys(e)
                .sort()
                .reduce((t, i) => ((t[i] = e[i]), t), {})
            : e
        );
      }
      function y(t, e) {
        return (
          t === e ||
          (typeof t == typeof e &&
            !!t &&
            !!e &&
            'object' == typeof t &&
            'object' == typeof e &&
            Object.keys(e).every((i) => y(t[i], e[i])))
        );
      }
      var v = Object.prototype.hasOwnProperty;
      function b(t, e) {
        if (!e || Object.keys(t).length !== Object.keys(e).length) return !1;
        for (let i in t) if (t[i] !== e[i]) return !1;
        return !0;
      }
      function m(t) {
        return Array.isArray(t) && t.length === Object.keys(t).length;
      }
      function g(t) {
        if (!S(t)) return !1;
        let e = t.constructor;
        if (void 0 === e) return !0;
        let i = e.prototype;
        return (
          !!S(i) &&
          !!i.hasOwnProperty('isPrototypeOf') &&
          Object.getPrototypeOf(t) === Object.prototype
        );
      }
      function S(t) {
        return '[object Object]' === Object.prototype.toString.call(t);
      }
      function w(t) {
        return new Promise((e) => {
          s.zs.setTimeout(e, t);
        });
      }
      function O(t, e, i) {
        return 'function' == typeof i.structuralSharing
          ? i.structuralSharing(t, e)
          : !1 !== i.structuralSharing
            ? (function t(e, i) {
                if (e === i) return e;
                let s = m(e) && m(i);
                if (!s && !(g(e) && g(i))) return i;
                let r = (s ? e : Object.keys(e)).length,
                  n = s ? i : Object.keys(i),
                  o = n.length,
                  a = s ? Array(o) : {},
                  u = 0;
                for (let c = 0; c < o; c++) {
                  let o = s ? c : n[c],
                    h = e[o],
                    l = i[o];
                  if (h === l) {
                    ((a[o] = h), (s ? c < r : v.call(e, o)) && u++);
                    continue;
                  }
                  if (
                    null === h ||
                    null === l ||
                    'object' != typeof h ||
                    'object' != typeof l
                  ) {
                    a[o] = l;
                    continue;
                  }
                  let d = t(h, l);
                  ((a[o] = d), d === h && u++);
                }
                return r === o && u === r ? e : a;
              })(t, e)
            : e;
      }
      function C(t, e, i = 0) {
        let s = [...t, e];
        return i && s.length > i ? s.slice(1) : s;
      }
      function F(t, e, i = 0) {
        let s = [e, ...t];
        return i && s.length > i ? s.slice(0, -1) : s;
      }
      var T = Symbol();
      function j(t, e) {
        return !t.queryFn && e?.initialPromise
          ? () => e.initialPromise
          : t.queryFn && t.queryFn !== T
            ? t.queryFn
            : () => Promise.reject(Error(`Missing queryFn: '${t.queryHash}'`));
      }
      function E(t, e) {
        return 'function' == typeof t ? t(...e) : !!t;
      }
    },
  },
]);
