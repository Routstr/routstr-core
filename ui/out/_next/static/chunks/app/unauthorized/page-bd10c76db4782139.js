(self.webpackChunk_N_E = self.webpackChunk_N_E || []).push([
  [305],
  {
    1064: (e, r, t) => {
      'use strict';
      var s = t(8);
      (t.o(s, 'usePathname') &&
        t.d(r, {
          usePathname: function () {
            return s.usePathname;
          },
        }),
        t.o(s, 'useRouter') &&
          t.d(r, {
            useRouter: function () {
              return s.useRouter;
            },
          }));
    },
    2818: (e, r, t) => {
      'use strict';
      (t.r(r), t.d(r, { default: () => o }));
      var s = t(3422),
        a = t(3648),
        i = t(1064);
      let n = (0, t(3929).A)('shield-alert', [
        [
          'path',
          {
            d: 'M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z',
            key: 'oel41y',
          },
        ],
        ['path', { d: 'M12 8v4', key: '1got3b' }],
        ['path', { d: 'M12 16h.01', key: '1drbdi' }],
      ]);
      function o() {
        let e = (0, i.useRouter)();
        return (0, s.jsx)('div', {
          className:
            'bg-background flex min-h-screen flex-col items-center justify-center p-4',
          children: (0, s.jsxs)('div', {
            className:
              'flex max-w-md flex-col items-center space-y-6 text-center',
            children: [
              (0, s.jsx)(n, { className: 'text-destructive h-24 w-24' }),
              (0, s.jsx)('h1', {
                className: 'text-4xl font-bold',
                children: 'Access Denied',
              }),
              (0, s.jsx)('p', {
                className: 'text-muted-foreground text-lg',
                children:
                  "You don't have permission to access this page. Please contact your administrator if you believe this is an error.",
              }),
              (0, s.jsxs)('div', {
                className: 'flex gap-4',
                children: [
                  (0, s.jsx)(a.$, {
                    onClick: () => e.push('/'),
                    children: 'Go to Dashboard',
                  }),
                  (0, s.jsx)(a.$, {
                    variant: 'outline',
                    onClick: () => e.back(),
                    children: 'Go Back',
                  }),
                ],
              }),
            ],
          }),
        });
      }
    },
    3648: (e, r, t) => {
      'use strict';
      t.d(r, { $: () => d, r: () => o });
      var s = t(3422);
      t(4398);
      var a = t(6950),
        i = t(4676),
        n = t(9183);
      let o = (0, i.F)(
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
      function d(e) {
        let { className: r, variant: t, size: i, asChild: d = !1, ...c } = e,
          l = d ? a.DX : 'button';
        return (0, s.jsx)(l, {
          'data-slot': 'button',
          className: (0, n.cn)(o({ variant: t, size: i, className: r })),
          ...c,
        });
      }
    },
    3929: (e, r, t) => {
      'use strict';
      t.d(r, { A: () => u });
      var s = t(4398);
      let a = (e) => e.replace(/([a-z0-9])([A-Z])/g, '$1-$2').toLowerCase(),
        i = (e) =>
          e.replace(/^([A-Z])|[\s-_]+(\w)/g, (e, r, t) =>
            t ? t.toUpperCase() : r.toLowerCase()
          ),
        n = (e) => {
          let r = i(e);
          return r.charAt(0).toUpperCase() + r.slice(1);
        },
        o = function () {
          for (var e = arguments.length, r = Array(e), t = 0; t < e; t++)
            r[t] = arguments[t];
          return r
            .filter((e, r, t) => !!e && '' !== e.trim() && t.indexOf(e) === r)
            .join(' ')
            .trim();
        },
        d = (e) => {
          for (let r in e)
            if (r.startsWith('aria-') || 'role' === r || 'title' === r)
              return !0;
        };
      var c = {
        xmlns: 'http://www.w3.org/2000/svg',
        width: 24,
        height: 24,
        viewBox: '0 0 24 24',
        fill: 'none',
        stroke: 'currentColor',
        strokeWidth: 2,
        strokeLinecap: 'round',
        strokeLinejoin: 'round',
      };
      let l = (0, s.forwardRef)((e, r) => {
          let {
            color: t = 'currentColor',
            size: a = 24,
            strokeWidth: i = 2,
            absoluteStrokeWidth: n,
            className: l = '',
            children: u,
            iconNode: h,
            ...v
          } = e;
          return (0, s.createElement)(
            'svg',
            {
              ref: r,
              ...c,
              width: a,
              height: a,
              stroke: t,
              strokeWidth: n ? (24 * Number(i)) / Number(a) : i,
              className: o('lucide', l),
              ...(!u && !d(v) && { 'aria-hidden': 'true' }),
              ...v,
            },
            [
              ...h.map((e) => {
                let [r, t] = e;
                return (0, s.createElement)(r, t);
              }),
              ...(Array.isArray(u) ? u : [u]),
            ]
          );
        }),
        u = (e, r) => {
          let t = (0, s.forwardRef)((t, i) => {
            let { className: d, ...c } = t;
            return (0, s.createElement)(l, {
              ref: i,
              iconNode: r,
              className: o('lucide-'.concat(a(n(e))), 'lucide-'.concat(e), d),
              ...c,
            });
          });
          return ((t.displayName = n(e)), t);
        };
    },
    6090: (e, r, t) => {
      Promise.resolve().then(t.bind(t, 2818));
    },
    9183: (e, r, t) => {
      'use strict';
      t.d(r, { cn: () => i });
      var s = t(8082),
        a = t(4966);
      function i() {
        for (var e = arguments.length, r = Array(e), t = 0; t < e; t++)
          r[t] = arguments[t];
        return (0, a.QP)((0, s.$)(r));
      }
    },
  },
  (e) => {
    var r = (r) => e((e.s = r));
    (e.O(0, [222, 945, 497, 358], () => r(6090)), (_N_E = e.O()));
  },
]);
