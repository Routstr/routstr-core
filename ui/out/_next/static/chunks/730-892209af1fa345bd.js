'use strict';
(self.webpackChunk_N_E = self.webpackChunk_N_E || []).push([
  [730],
  {
    2349: (e, t, r) => {
      r.d(t, { I: () => x });
      var s = r(6479),
        i = r(5688),
        n = r(8192),
        u = r(3145),
        a = r(7623),
        h = r(9205),
        c = r(8890),
        l = class extends u.Q {
          constructor(e, t) {
            (super(),
              (this.options = t),
              (this.#e = e),
              (this.#t = null),
              (this.#r = (0, a.T)()),
              this.bindMethods(),
              this.setOptions(t));
          }
          #e;
          #s = void 0;
          #i = void 0;
          #n = void 0;
          #u;
          #a;
          #r;
          #t;
          #h;
          #c;
          #l;
          #o;
          #d;
          #p;
          #f = new Set();
          bindMethods() {
            this.refetch = this.refetch.bind(this);
          }
          onSubscribe() {
            1 === this.listeners.size &&
              (this.#s.addObserver(this),
              o(this.#s, this.options) ? this.#y() : this.updateResult(),
              this.#R());
          }
          onUnsubscribe() {
            this.hasListeners() || this.destroy();
          }
          shouldFetchOnReconnect() {
            return d(this.#s, this.options, this.options.refetchOnReconnect);
          }
          shouldFetchOnWindowFocus() {
            return d(this.#s, this.options, this.options.refetchOnWindowFocus);
          }
          destroy() {
            ((this.listeners = new Set()),
              this.#Q(),
              this.#b(),
              this.#s.removeObserver(this));
          }
          setOptions(e) {
            let t = this.options,
              r = this.#s;
            if (
              ((this.options = this.#e.defaultQueryOptions(e)),
              void 0 !== this.options.enabled &&
                'boolean' != typeof this.options.enabled &&
                'function' != typeof this.options.enabled &&
                'boolean' != typeof (0, h.Eh)(this.options.enabled, this.#s))
            )
              throw Error(
                'Expected enabled to be a boolean or a callback that returns a boolean'
              );
            (this.#v(),
              this.#s.setOptions(this.options),
              t._defaulted &&
                !(0, h.f8)(this.options, t) &&
                this.#e
                  .getQueryCache()
                  .notify({
                    type: 'observerOptionsUpdated',
                    query: this.#s,
                    observer: this,
                  }));
            let s = this.hasListeners();
            (s && p(this.#s, r, this.options, t) && this.#y(),
              this.updateResult(),
              s &&
                (this.#s !== r ||
                  (0, h.Eh)(this.options.enabled, this.#s) !==
                    (0, h.Eh)(t.enabled, this.#s) ||
                  (0, h.d2)(this.options.staleTime, this.#s) !==
                    (0, h.d2)(t.staleTime, this.#s)) &&
                this.#m());
            let i = this.#I();
            s &&
              (this.#s !== r ||
                (0, h.Eh)(this.options.enabled, this.#s) !==
                  (0, h.Eh)(t.enabled, this.#s) ||
                i !== this.#p) &&
              this.#g(i);
          }
          getOptimisticResult(e) {
            var t, r;
            let s = this.#e.getQueryCache().build(this.#e, e),
              i = this.createResult(s, e);
            return (
              (t = this),
              (r = i),
              (0, h.f8)(t.getCurrentResult(), r) ||
                ((this.#n = i),
                (this.#a = this.options),
                (this.#u = this.#s.state)),
              i
            );
          }
          getCurrentResult() {
            return this.#n;
          }
          trackResult(e, t) {
            return new Proxy(e, {
              get: (e, r) => (
                this.trackProp(r),
                t?.(r),
                'promise' === r &&
                  (this.trackProp('data'),
                  this.options.experimental_prefetchInRender ||
                    'pending' !== this.#r.status ||
                    this.#r.reject(
                      Error(
                        'experimental_prefetchInRender feature flag is not enabled'
                      )
                    )),
                Reflect.get(e, r)
              ),
            });
          }
          trackProp(e) {
            this.#f.add(e);
          }
          getCurrentQuery() {
            return this.#s;
          }
          refetch({ ...e } = {}) {
            return this.fetch({ ...e });
          }
          fetchOptimistic(e) {
            let t = this.#e.defaultQueryOptions(e),
              r = this.#e.getQueryCache().build(this.#e, t);
            return r.fetch().then(() => this.createResult(r, t));
          }
          fetch(e) {
            return this.#y({ ...e, cancelRefetch: e.cancelRefetch ?? !0 }).then(
              () => (this.updateResult(), this.#n)
            );
          }
          #y(e) {
            this.#v();
            let t = this.#s.fetch(this.options, e);
            return (e?.throwOnError || (t = t.catch(h.lQ)), t);
          }
          #m() {
            this.#Q();
            let e = (0, h.d2)(this.options.staleTime, this.#s);
            if (h.S$ || this.#n.isStale || !(0, h.gn)(e)) return;
            let t = (0, h.j3)(this.#n.dataUpdatedAt, e);
            this.#o = c.zs.setTimeout(() => {
              this.#n.isStale || this.updateResult();
            }, t + 1);
          }
          #I() {
            return (
              ('function' == typeof this.options.refetchInterval
                ? this.options.refetchInterval(this.#s)
                : this.options.refetchInterval) ?? !1
            );
          }
          #g(e) {
            (this.#b(),
              (this.#p = e),
              !h.S$ &&
                !1 !== (0, h.Eh)(this.options.enabled, this.#s) &&
                (0, h.gn)(this.#p) &&
                0 !== this.#p &&
                (this.#d = c.zs.setInterval(() => {
                  (this.options.refetchIntervalInBackground ||
                    s.m.isFocused()) &&
                    this.#y();
                }, this.#p)));
          }
          #R() {
            (this.#m(), this.#g(this.#I()));
          }
          #Q() {
            this.#o && (c.zs.clearTimeout(this.#o), (this.#o = void 0));
          }
          #b() {
            this.#d && (c.zs.clearInterval(this.#d), (this.#d = void 0));
          }
          createResult(e, t) {
            let r,
              s = this.#s,
              i = this.options,
              u = this.#n,
              c = this.#u,
              l = this.#a,
              d = e !== s ? e.state : this.#i,
              { state: y } = e,
              R = { ...y },
              Q = !1;
            if (t._optimisticResults) {
              let r = this.hasListeners(),
                u = !r && o(e, t),
                a = r && p(e, s, t, i);
              ((u || a) && (R = { ...R, ...(0, n.k)(y.data, e.options) }),
                'isRestoring' === t._optimisticResults &&
                  (R.fetchStatus = 'idle'));
            }
            let { error: b, errorUpdatedAt: v, status: m } = R;
            r = R.data;
            let I = !1;
            if (
              void 0 !== t.placeholderData &&
              void 0 === r &&
              'pending' === m
            ) {
              let e;
              (u?.isPlaceholderData && t.placeholderData === l?.placeholderData
                ? ((e = u.data), (I = !0))
                : (e =
                    'function' == typeof t.placeholderData
                      ? t.placeholderData(this.#l?.state.data, this.#l)
                      : t.placeholderData),
                void 0 !== e &&
                  ((m = 'success'), (r = (0, h.pl)(u?.data, e, t)), (Q = !0)));
            }
            if (t.select && void 0 !== r && !I)
              if (u && r === c?.data && t.select === this.#h) r = this.#c;
              else
                try {
                  ((this.#h = t.select),
                    (r = t.select(r)),
                    (r = (0, h.pl)(u?.data, r, t)),
                    (this.#c = r),
                    (this.#t = null));
                } catch (e) {
                  this.#t = e;
                }
            this.#t &&
              ((b = this.#t), (r = this.#c), (v = Date.now()), (m = 'error'));
            let g = 'fetching' === R.fetchStatus,
              E = 'pending' === m,
              O = 'error' === m,
              T = E && g,
              S = void 0 !== r,
              C = {
                status: m,
                fetchStatus: R.fetchStatus,
                isPending: E,
                isSuccess: 'success' === m,
                isError: O,
                isInitialLoading: T,
                isLoading: T,
                data: r,
                dataUpdatedAt: R.dataUpdatedAt,
                error: b,
                errorUpdatedAt: v,
                failureCount: R.fetchFailureCount,
                failureReason: R.fetchFailureReason,
                errorUpdateCount: R.errorUpdateCount,
                isFetched: R.dataUpdateCount > 0 || R.errorUpdateCount > 0,
                isFetchedAfterMount:
                  R.dataUpdateCount > d.dataUpdateCount ||
                  R.errorUpdateCount > d.errorUpdateCount,
                isFetching: g,
                isRefetching: g && !E,
                isLoadingError: O && !S,
                isPaused: 'paused' === R.fetchStatus,
                isPlaceholderData: Q,
                isRefetchError: O && S,
                isStale: f(e, t),
                refetch: this.refetch,
                promise: this.#r,
                isEnabled: !1 !== (0, h.Eh)(t.enabled, e),
              };
            if (this.options.experimental_prefetchInRender) {
              let t = (e) => {
                  'error' === C.status
                    ? e.reject(C.error)
                    : void 0 !== C.data && e.resolve(C.data);
                },
                r = () => {
                  t((this.#r = C.promise = (0, a.T)()));
                },
                i = this.#r;
              switch (i.status) {
                case 'pending':
                  e.queryHash === s.queryHash && t(i);
                  break;
                case 'fulfilled':
                  ('error' === C.status || C.data !== i.value) && r();
                  break;
                case 'rejected':
                  ('error' !== C.status || C.error !== i.reason) && r();
              }
            }
            return C;
          }
          updateResult() {
            let e = this.#n,
              t = this.createResult(this.#s, this.options);
            ((this.#u = this.#s.state),
              (this.#a = this.options),
              void 0 !== this.#u.data && (this.#l = this.#s),
              (0, h.f8)(t, e) ||
                ((this.#n = t),
                this.#E({
                  listeners: (() => {
                    if (!e) return !0;
                    let { notifyOnChangeProps: t } = this.options,
                      r = 'function' == typeof t ? t() : t;
                    if ('all' === r || (!r && !this.#f.size)) return !0;
                    let s = new Set(r ?? this.#f);
                    return (
                      this.options.throwOnError && s.add('error'),
                      Object.keys(this.#n).some(
                        (t) => this.#n[t] !== e[t] && s.has(t)
                      )
                    );
                  })(),
                })));
          }
          #v() {
            let e = this.#e.getQueryCache().build(this.#e, this.options);
            if (e === this.#s) return;
            let t = this.#s;
            ((this.#s = e),
              (this.#i = e.state),
              this.hasListeners() &&
                (t?.removeObserver(this), e.addObserver(this)));
          }
          onQueryUpdate() {
            (this.updateResult(), this.hasListeners() && this.#R());
          }
          #E(e) {
            i.jG.batch(() => {
              (e.listeners &&
                this.listeners.forEach((e) => {
                  e(this.#n);
                }),
                this.#e
                  .getQueryCache()
                  .notify({ query: this.#s, type: 'observerResultsUpdated' }));
            });
          }
        };
      function o(e, t) {
        return (
          (!1 !== (0, h.Eh)(t.enabled, e) &&
            void 0 === e.state.data &&
            ('error' !== e.state.status || !1 !== t.retryOnMount)) ||
          (void 0 !== e.state.data && d(e, t, t.refetchOnMount))
        );
      }
      function d(e, t, r) {
        if (
          !1 !== (0, h.Eh)(t.enabled, e) &&
          'static' !== (0, h.d2)(t.staleTime, e)
        ) {
          let s = 'function' == typeof r ? r(e) : r;
          return 'always' === s || (!1 !== s && f(e, t));
        }
        return !1;
      }
      function p(e, t, r, s) {
        return (
          (e !== t || !1 === (0, h.Eh)(s.enabled, e)) &&
          (!r.suspense || 'error' !== e.state.status) &&
          f(e, r)
        );
      }
      function f(e, t) {
        return (
          !1 !== (0, h.Eh)(t.enabled, e) &&
          e.isStaleByTime((0, h.d2)(t.staleTime, e))
        );
      }
      var y = r(4398),
        R = r(100);
      r(3422);
      var Q = y.createContext(
          (function () {
            let e = !1;
            return {
              clearReset: () => {
                e = !1;
              },
              reset: () => {
                e = !0;
              },
              isReset: () => e,
            };
          })()
        ),
        b = () => y.useContext(Q),
        v = (e, t) => {
          (e.suspense || e.throwOnError || e.experimental_prefetchInRender) &&
            !t.isReset() &&
            (e.retryOnMount = !1);
        },
        m = (e) => {
          y.useEffect(() => {
            e.clearReset();
          }, [e]);
        },
        I = (e) => {
          let {
            result: t,
            errorResetBoundary: r,
            throwOnError: s,
            query: i,
            suspense: n,
          } = e;
          return (
            t.isError &&
            !r.isReset() &&
            !t.isFetching &&
            i &&
            ((n && void 0 === t.data) || (0, h.GU)(s, [t.error, i]))
          );
        },
        g = y.createContext(!1),
        E = () => y.useContext(g);
      g.Provider;
      var O = (e) => {
          if (e.suspense) {
            let t = (e) => ('static' === e ? e : Math.max(e ?? 1e3, 1e3)),
              r = e.staleTime;
            ((e.staleTime =
              'function' == typeof r ? (...e) => t(r(...e)) : t(r)),
              'number' == typeof e.gcTime &&
                (e.gcTime = Math.max(e.gcTime, 1e3)));
          }
        },
        T = (e, t) => e.isLoading && e.isFetching && !t,
        S = (e, t) => e?.suspense && t.isPending,
        C = (e, t, r) =>
          t.fetchOptimistic(e).catch(() => {
            r.clearReset();
          });
      function x(e, t) {
        return (function (e, t, r) {
          var s, n, u, a, c;
          let l = E(),
            o = b(),
            d = (0, R.jE)(r),
            p = d.defaultQueryOptions(e);
          (null == (n = d.getDefaultOptions().queries) ||
            null == (s = n._experimental_beforeQuery) ||
            s.call(n, p),
            (p._optimisticResults = l ? 'isRestoring' : 'optimistic'),
            O(p),
            v(p, o),
            m(o));
          let f = !d.getQueryCache().get(p.queryHash),
            [Q] = y.useState(() => new t(d, p)),
            g = Q.getOptimisticResult(p),
            x = !l && !1 !== e.subscribed;
          if (
            (y.useSyncExternalStore(
              y.useCallback(
                (e) => {
                  let t = x ? Q.subscribe(i.jG.batchCalls(e)) : h.lQ;
                  return (Q.updateResult(), t);
                },
                [Q, x]
              ),
              () => Q.getCurrentResult(),
              () => Q.getCurrentResult()
            ),
            y.useEffect(() => {
              Q.setOptions(p);
            }, [p, Q]),
            S(p, g))
          )
            throw C(p, Q, o);
          if (
            I({
              result: g,
              errorResetBoundary: o,
              throwOnError: p.throwOnError,
              query: d.getQueryCache().get(p.queryHash),
              suspense: p.suspense,
            })
          )
            throw g.error;
          if (
            (null == (a = d.getDefaultOptions().queries) ||
              null == (u = a._experimental_afterQuery) ||
              u.call(a, p, g),
            p.experimental_prefetchInRender && !h.S$ && T(g, l))
          ) {
            let e = f
              ? C(p, Q, o)
              : null == (c = d.getQueryCache().get(p.queryHash))
                ? void 0
                : c.promise;
            null == e ||
              e.catch(h.lQ).finally(() => {
                Q.updateResult();
              });
          }
          return p.notifyOnChangeProps ? g : Q.trackResult(g);
        })(e, l, t);
      }
    },
  },
]);
