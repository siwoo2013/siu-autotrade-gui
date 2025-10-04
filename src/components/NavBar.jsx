export default function NavBar() {
  return (
    <nav className="nav">
      {['Home', 'Event', 'Invest', 'Service', 'MY'].map((tab) => (
        <button key={tab}>{tab}</button>
      ))}
    </nav>
  )
}