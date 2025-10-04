import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './style.css'

const saved = localStorage.getItem('theme')
const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
const startDark = saved ? saved === 'dark' : prefersDark
if (startDark) document.documentElement.classList.add('dark')

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)