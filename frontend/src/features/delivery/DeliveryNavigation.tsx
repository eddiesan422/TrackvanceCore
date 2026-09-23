import { NavLink } from 'react-router-dom'
import { Send, Server } from 'lucide-react'

export function DeliveryNavigation() {
  return <nav className="delivery-navigation" aria-label="Secciones de Data Delivery"><NavLink to="/delivery" end className={({ isActive }) => isActive ? 'active' : ''}><Send size={16}/> Entregas</NavLink><NavLink to="/delivery/destinations" className={({ isActive }) => isActive ? 'active' : ''}><Server size={16}/> Destinos</NavLink></nav>
}
