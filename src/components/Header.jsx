import ThemeToggle from './ThemeToggle'

export default function Header() {
  return (
    <header className="header">
      <div style={{display:'flex', alignItems:'center', justifyContent:'space-between', maxWidth:860, margin:'0 auto'}}>
        <div style={{width:90, textAlign:'left'}}><ThemeToggle/></div>
        <div>
          <h3 style={{margin:'6px 0'}}>MINI GOLD NFT</h3>
          <p style={{ fontSize: 12, color:'var(--muted)', margin:0 }}>홍길동 3***5386</p>
        </div>
        <div style={{width:90}} />
      </div>
    </header>
  )
}