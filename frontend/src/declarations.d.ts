// JSX declarations for the Material Web custom elements used in this app.
// TypeScript rejects unknown JSX tags, so every <md-*> tag rendered anywhere
// must be declared here. Props are typed as the element's reactive
// properties — React 19 assigns a prop as a property when one exists on the
// element (hyphenated props like supporting-text become attributes).
import type { DetailedHTMLProps, HTMLAttributes } from 'react'

interface MdTextFieldElement extends HTMLElement {
  value: string
  disabled: boolean
}

interface MdSelectElement extends HTMLElement {
  value: string
}

type MdProps<T extends HTMLElement = HTMLElement, Extra = object> = DetailedHTMLProps<
  HTMLAttributes<T>,
  T
> &
  Extra

type MdButtonProps = MdProps<
  HTMLElement,
  { disabled?: boolean; type?: 'button' | 'submit' | 'reset' }
>

declare module 'react' {
  namespace JSX {
    interface IntrinsicElements {
      'md-filled-button': MdButtonProps
      'md-filled-tonal-button': MdButtonProps
      'md-outlined-button': MdButtonProps
      'md-text-button': MdButtonProps
      'md-divider': MdProps
      'md-list': MdProps
      'md-list-item': MdProps<HTMLElement, { type?: 'text' | 'button' | 'link' }>
      'md-linear-progress': MdProps<HTMLElement, { value?: number; max?: number }>
      'md-outlined-select': MdProps<
        MdSelectElement,
        { label?: string; value?: string; disabled?: boolean }
      >
      'md-select-option': MdProps<HTMLElement, { value?: string; selected?: boolean }>
      'md-outlined-text-field': MdProps<
        MdTextFieldElement,
        {
          label?: string
          value?: string
          type?: string
          rows?: number
          placeholder?: string
          disabled?: boolean
          'supporting-text'?: string
        }
      >
    }
  }
}

export type { MdSelectElement, MdTextFieldElement }
