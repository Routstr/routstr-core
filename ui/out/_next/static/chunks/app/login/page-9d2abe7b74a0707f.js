(self.webpackChunk_N_E = self.webpackChunk_N_E || []).push([
  [520],
  {
    268: (e, t, r) => {
      'use strict';
      r.d(t, { p: () => o });
      var a = r(3422);
      r(4398);
      var n = r(9183);
      function o(e) {
        let { className: t, type: r, ...o } = e;
        return (0, a.jsx)('input', {
          type: r,
          'data-slot': 'input',
          className: (0, n.cn)(
            'file:text-foreground placeholder:text-muted-foreground selection:bg-primary selection:text-primary-foreground dark:bg-input/30 border-input flex h-9 w-full min-w-0 rounded-md border bg-transparent px-3 py-1 text-base shadow-xs transition-[color,box-shadow] outline-none file:inline-flex file:h-7 file:border-0 file:bg-transparent file:text-sm file:font-medium disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 md:text-sm',
            'focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]',
            'aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 aria-invalid:border-destructive',
            t
          ),
          ...o,
        });
      }
    },
    2738: (e, t, r) => {
      'use strict';
      r.d(t, {
        BT: () => c,
        Wu: () => l,
        ZB: () => i,
        Zp: () => o,
        aR: () => s,
        wL: () => d,
      });
      var a = r(3422);
      r(4398);
      var n = r(9183);
      function o(e) {
        let { className: t, ...r } = e;
        return (0, a.jsx)('div', {
          'data-slot': 'card',
          className: (0, n.cn)(
            'bg-card text-card-foreground flex flex-col gap-6 rounded-xl border py-6 shadow-sm',
            t
          ),
          ...r,
        });
      }
      function s(e) {
        let { className: t, ...r } = e;
        return (0, a.jsx)('div', {
          'data-slot': 'card-header',
          className: (0, n.cn)(
            '@container/card-header grid auto-rows-min grid-rows-[auto_auto] items-start gap-1.5 px-6 has-data-[slot=card-action]:grid-cols-[1fr_auto] [.border-b]:pb-6',
            t
          ),
          ...r,
        });
      }
      function i(e) {
        let { className: t, ...r } = e;
        return (0, a.jsx)('div', {
          'data-slot': 'card-title',
          className: (0, n.cn)('leading-none font-semibold', t),
          ...r,
        });
      }
      function c(e) {
        let { className: t, ...r } = e;
        return (0, a.jsx)('div', {
          'data-slot': 'card-description',
          className: (0, n.cn)('text-muted-foreground text-sm', t),
          ...r,
        });
      }
      function l(e) {
        let { className: t, ...r } = e;
        return (0, a.jsx)('div', {
          'data-slot': 'card-content',
          className: (0, n.cn)('px-6', t),
          ...r,
        });
      }
      function d(e) {
        let { className: t, ...r } = e;
        return (0, a.jsx)('div', {
          'data-slot': 'card-footer',
          className: (0, n.cn)('flex items-center px-6 [.border-t]:pt-6', t),
          ...r,
        });
      }
    },
    3234: (e, t, r) => {
      'use strict';
      r.d(t, { s: () => o });
      var a = r(9945),
        n = r(5842);
      a.Ik({
        endpoint: a.Yj().url().or(a.eu('')),
        apiKey: a.Yj(),
        enabled: a.zM(),
      });
      class o {
        static getServerConfig() {
          return {
            endpoint: localStorage.getItem('server_endpoint') || '',
            apiKey: localStorage.getItem('server_api_key') || '',
            enabled: 'true' === localStorage.getItem('server_enabled'),
          };
        }
        static saveServerConfig(e) {
          (localStorage.setItem('server_endpoint', e.endpoint),
            localStorage.setItem('server_api_key', e.apiKey),
            localStorage.setItem('server_enabled', e.enabled.toString()));
        }
        static isServerConfigValid() {
          let e = this.getServerConfig();
          return e.enabled && !!e.endpoint && !!e.apiKey;
        }
        static getLocalBaseUrl() {
          return 'http://127.0.0.1:8000';
        }
        static getBaseUrl() {
          let e = this.getServerConfig();
          return e.enabled && e.endpoint ? e.endpoint : this.getLocalBaseUrl();
        }
        static getAuthHeaders() {
          let e = { 'Content-Type': 'application/json' };
          try {
            {
              let t = localStorage.getItem('admin_token'),
                r = localStorage.getItem('admin_token_expiry');
              if (t && r) {
                let a = parseInt(r, 10);
                if (Date.now() < a)
                  return ((e.Authorization = 'Bearer '.concat(t)), e);
                (localStorage.removeItem('admin_token'),
                  localStorage.removeItem('admin_token_expiry'));
              }
            }
          } catch (e) {
            console.warn('Error accessing localStorage:', e);
          }
          let t = 'dummy-admin-key-12345';
          return (t && (e.Authorization = 'Bearer '.concat(t)), e);
        }
        static isTokenValid() {
          let e = localStorage.getItem('admin_token'),
            t = localStorage.getItem('admin_token_expiry');
          if (!e || !t) return !1;
          let r = parseInt(t, 10);
          return Date.now() < r;
        }
        static clearToken() {
          (localStorage.removeItem('admin_token'),
            localStorage.removeItem('admin_token_expiry'));
        }
        static setToken(e, t) {
          let r = Date.now() + 1e3 * t;
          (localStorage.setItem('admin_token', e),
            localStorage.setItem('admin_token_expiry', String(r)));
        }
        static async testConnection(e) {
          if (!e.endpoint) return !1;
          try {
            let t = await n.A.get(''.concat(e.endpoint, '/health'), {
              headers: { 'Content-Type': 'application/json' },
              timeout: 1e3,
            });
            return 200 === t.status;
          } catch (e) {
            return (console.error('Server connection test failed:', e), !1);
          }
        }
        static async saveServerConfigToBackend(e) {
          try {
            this.saveServerConfig(e);
            let t = this.getLocalBaseUrl(),
              r = { endpoint: e.endpoint, api_key: e.apiKey };
            return (
              await n.A.post(''.concat(t, '/api/server-config'), r, {
                headers: { 'Content-Type': 'application/json' },
              }),
              !0
            );
          } catch (e) {
            return (
              console.error('Failed to save configuration to backend:', e),
              !1
            );
          }
        }
        static async loadServerConfigFromBackend() {
          try {
            let e = this.getLocalBaseUrl(),
              t = await n.A.get(''.concat(e, '/api/server-config'), {
                headers: { 'Content-Type': 'application/json' },
              });
            if (t && t.data) {
              let e = {
                endpoint: t.data.endpoint || '',
                apiKey: t.data.api_key || '',
                enabled: !!t.data.endpoint && !!t.data.api_key,
              };
              return (this.saveServerConfig(e), e);
            }
            return null;
          } catch (e) {
            return (
              console.error('Failed to load configuration from backend:', e),
              null
            );
          }
        }
        static async requestNewApiKey(e) {
          try {
            let t = this.getLocalBaseUrl(),
              r = await n.A.post(
                ''.concat(t, '/api/server-config/new-key'),
                { reason: e },
                {
                  headers: {
                    'Content-Type': 'application/json',
                    ...this.getAuthHeaders(),
                  },
                }
              );
            if (r.data && r.data.api_key) return r.data.api_key;
            throw Error('No API key received from server');
          } catch (e) {
            throw (console.error('Failed to request new API key:', e), e);
          }
        }
      }
    },
    3270: (e, t, r) => {
      'use strict';
      r.d(t, { bN: () => s, wu: () => i });
      var a = r(9945);
      r(7916);
      var n = r(3234),
        o = r(5842);
      async function s(e) {
        try {
          let t = n.s.getLocalBaseUrl(),
            r = await o.A.post(
              ''.concat(t, '/admin/api/login'),
              { password: e },
              { headers: { 'Content-Type': 'application/json' } }
            );
          return (
            r.data.token &&
              r.data.expires_in &&
              n.s.setToken(r.data.token, r.data.expires_in),
            r.data
          );
        } catch (e) {
          throw (console.error('Admin login error:', e), e);
        }
      }
      async function i() {
        try {
          let e = localStorage.getItem('admin_token');
          if (e) {
            let t = n.s.getLocalBaseUrl();
            await o.A.post(
              ''.concat(t, '/admin/api/logout'),
              {},
              {
                headers: {
                  'Content-Type': 'application/json',
                  Authorization: 'Bearer '.concat(e),
                },
              }
            );
          }
        } catch (e) {
          console.error('Admin logout error:', e);
        } finally {
          n.s.clearToken();
        }
      }
      (a.Ik({ username: a.Yj().optional(), password: a.Yj().optional() }),
        a.Ik({ id: a.Yj() }),
        a.Ik({ password: a.Yj().min(1, 'Password is required') }),
        a.Ik({ ok: a.zM(), token: a.Yj(), expires_in: a.ai() }),
        a.Ik({
          npub: a.Yj().min(10, { message: 'must have at least 10 character' }),
          name: a.Yj().optional(),
        }),
        a.Ik({ user_id: a.Yj(), theme: a.Yj() }));
    },
    3648: (e, t, r) => {
      'use strict';
      r.d(t, { $: () => c, r: () => i });
      var a = r(3422);
      r(4398);
      var n = r(6950),
        o = r(4676),
        s = r(9183);
      let i = (0, o.F)(
        "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-all disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg:not([class*='size-'])]:size-4 shrink-0 [&_svg]:shrink-0 outline-none focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 aria-invalid:border-destructive",
        {
          variants: {
            variant: {
              default:
                'bg-primary text-primary-foreground shadow-xs hover:bg-primary/90',
              destructive:
                'bg-destructive text-white shadow-xs hover:bg-destructive/90 focus-visible:ring-destructive/20 dark:focus-visible:ring-destructive/40 dark:bg-destructive/60',
              outline:
                'border bg-background shadow-xs hover:bg-accent hover:text-accent-foreground dark:bg-input/30 dark:border-input dark:hover:bg-input/50',
              secondary:
                'bg-secondary text-secondary-foreground shadow-xs hover:bg-secondary/80',
              ghost:
                'hover:bg-accent hover:text-accent-foreground dark:hover:bg-accent/50',
              link: 'text-primary underline-offset-4 hover:underline',
            },
            size: {
              default: 'h-9 px-4 py-2 has-[>svg]:px-3',
              sm: 'h-8 rounded-md gap-1.5 px-3 has-[>svg]:px-2.5',
              lg: 'h-10 rounded-md px-6 has-[>svg]:px-4',
              icon: 'size-9',
            },
          },
          defaultVariants: { variant: 'default', size: 'default' },
        }
      );
      function c(e) {
        let { className: t, variant: r, size: o, asChild: c = !1, ...l } = e,
          d = c ? n.DX : 'button';
        return (0, a.jsx)(d, {
          'data-slot': 'button',
          className: (0, s.cn)(i({ variant: r, size: o, className: t })),
          ...l,
        });
      }
    },
    7395: (e, t, r) => {
      Promise.resolve().then(r.bind(r, 9079));
    },
    7916: (e, t, r) => {
      'use strict';
      r.d(t, { apiClient: () => s });
      var a = r(5842),
        n = r(3234);
      class o {
        getBaseUrl() {
          return n.s.getLocalBaseUrl();
        }
        getHeaders() {
          return n.s.getAuthHeaders();
        }
        handleAuthError(e) {
          if (a.A.isAxiosError(e)) {
            var t, r;
            ((null == (t = e.response) ? void 0 : t.status) === 401 ||
              (null == (r = e.response) ? void 0 : r.status) === 403) &&
              (n.s.clearToken(), (window.location.href = '/login'));
          }
        }
        async get(e, t) {
          let r = {
            headers: this.getHeaders(),
            params: t,
            withCredentials: !1,
          };
          try {
            return (
              console.log(
                'Making GET request to '.concat(this.getBaseUrl()).concat(e)
              ),
              (await a.A.get(''.concat(this.getBaseUrl()).concat(e), r)).data
            );
          } catch (e) {
            throw (this.handleAuthError(e), e);
          }
        }
        async post(e, t) {
          let r = { headers: this.getHeaders(), withCredentials: !1 };
          try {
            return (
              console.log(
                'Making POST request to '.concat(this.getBaseUrl()).concat(e),
                t
              ),
              (await a.A.post(''.concat(this.getBaseUrl()).concat(e), t, r))
                .data
            );
          } catch (t) {
            throw (
              this.handleAuthError(t),
              console.error('Error posting to '.concat(e, ':'), t),
              t
            );
          }
        }
        async put(e, t) {
          let r = { headers: this.getHeaders(), withCredentials: !1 };
          try {
            return (
              console.log(
                'Making PUT request to '.concat(this.getBaseUrl()).concat(e),
                t
              ),
              (await a.A.put(''.concat(this.getBaseUrl()).concat(e), t, r)).data
            );
          } catch (t) {
            throw (
              this.handleAuthError(t),
              console.error('Error updating '.concat(e, ':'), t),
              t
            );
          }
        }
        async patch(e, t) {
          let r = { headers: this.getHeaders(), withCredentials: !1 };
          try {
            return (
              console.log(
                'Making PATCH request to '.concat(this.getBaseUrl()).concat(e),
                t
              ),
              (await a.A.patch(''.concat(this.getBaseUrl()).concat(e), t, r))
                .data
            );
          } catch (t) {
            throw (
              this.handleAuthError(t),
              console.error('Error patching '.concat(e, ':'), t),
              t
            );
          }
        }
        async delete(e) {
          let t = { headers: this.getHeaders(), withCredentials: !1 };
          try {
            return (
              console.log(
                'Making DELETE request to '.concat(this.getBaseUrl()).concat(e)
              ),
              (await a.A.delete(''.concat(this.getBaseUrl()).concat(e), t)).data
            );
          } catch (t) {
            throw (
              this.handleAuthError(t),
              console.error('Error deleting from '.concat(e, ':'), t),
              t
            );
          }
        }
      }
      let s = new o();
    },
    9079: (e, t, r) => {
      'use strict';
      (r.r(t), r.d(t, { default: () => g }));
      var a = r(3422),
        n = r(4398),
        o = r(1064),
        s = r(3648),
        i = r(268),
        c = r(2738),
        l = r(3270),
        d = r(3234),
        u = r(3831);
      function g() {
        let e = (0, o.useRouter)(),
          [t, r] = (0, n.useState)(''),
          [g, p] = (0, n.useState)(!1);
        (0, n.useEffect)(() => {
          d.s.isTokenValid() && e.push('/');
        }, [e]);
        let h = async (r) => {
          if ((r.preventDefault(), !t))
            return void u.oR.error('Please enter your password');
          p(!0);
          try {
            (await (0, l.bN)(t),
              u.oR.success('Successfully logged in'),
              e.push('/'));
          } catch (e) {
            (console.error('Login error:', e),
              u.oR.error('Invalid password. Please try again.'));
          } finally {
            p(!1);
          }
        };
        return (0, a.jsx)('div', {
          className:
            'flex min-h-screen items-center justify-center bg-gray-50 p-4',
          children: (0, a.jsxs)(c.Zp, {
            className: 'w-full max-w-md',
            children: [
              (0, a.jsxs)(c.aR, {
                className: 'space-y-1',
                children: [
                  (0, a.jsx)(c.ZB, {
                    className: 'text-center text-2xl font-bold',
                    children: 'Admin Login',
                  }),
                  (0, a.jsx)(c.BT, {
                    className: 'text-center',
                    children:
                      'Enter your admin password to access the dashboard',
                  }),
                ],
              }),
              (0, a.jsx)(c.Wu, {
                children: (0, a.jsxs)('form', {
                  onSubmit: h,
                  className: 'space-y-4',
                  children: [
                    (0, a.jsx)('div', {
                      className: 'space-y-2',
                      children: (0, a.jsx)(i.p, {
                        type: 'password',
                        placeholder: 'Admin Password',
                        value: t,
                        onChange: (e) => r(e.target.value),
                        disabled: g,
                        autoFocus: !0,
                        required: !0,
                      }),
                    }),
                    (0, a.jsx)(s.$, {
                      type: 'submit',
                      className: 'w-full',
                      disabled: g,
                      children: g ? 'Logging in...' : 'Login',
                    }),
                  ],
                }),
              }),
            ],
          }),
        });
      }
    },
    9183: (e, t, r) => {
      'use strict';
      r.d(t, { cn: () => o });
      var a = r(8082),
        n = r(4966);
      function o() {
        for (var e = arguments.length, t = Array(e), r = 0; r < e; r++)
          t[r] = arguments[r];
        return (0, n.QP)((0, a.$)(t));
      }
    },
  },
  (e) => {
    var t = (t) => e((e.s = t));
    (e.O(0, [363, 222, 945, 497, 358], () => t(7395)), (_N_E = e.O()));
  },
]);
