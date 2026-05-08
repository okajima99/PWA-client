import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores([
    'dist',
    // cap sync で copy される ipa 内 dist。 lint 対象外。
    'ios/App/App/public',
  ]),
  {
    files: ['**/*.{js,jsx}'],
    extends: [
      js.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
      parserOptions: {
        ecmaVersion: 'latest',
        ecmaFeatures: { jsx: true },
        sourceType: 'module',
      },
    },
    rules: {
      'no-unused-vars': ['error', { varsIgnorePattern: '^[A-Z_]' }],
      // 観測値を effect 内で蓄積する pattern (= setSessionActivity / setLastSeenLen で
      // messages の length を見て derive) は副作用ないので許容する。 React 19 の
      // 厳格ルールでは false-positive。
      'react-hooks/set-state-in-effect': 'off',
      // main.jsx で initial bridge state を window に export する import + 同 file 内
      // App render の併用は HMR のみ影響、 prod 動作に問題なし。
      'react-refresh/only-export-components': 'off',
    },
  },
])
