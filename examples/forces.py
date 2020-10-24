import ase
from ase.visualize import view
from ase.calculators.lj import LennardJones
import numpy as np
from dscribe.descriptors import SOAP

species = ["H"]
rcut = 3.0
nmax = 3
lmax = 2

# Setting up the SOAP descriptor
soap = SOAP(
    species=species,
    periodic=False,
    rcut=rcut,
    nmax=nmax,
    lmax=lmax,
)

# center is origin
CENTER = [0,0,0]

# one example of h2 molecule
d = 2
atoms = ase.Atoms('HH', positions = [[-0.5 * d, 0, 0], [0.5 * d, 0, 0]])
#view(atoms)
atoms.set_calculator(LennardJones(epsilon = 1e-10 , sigma = 5.0, rc = 10.0))
forces = atoms.get_forces()
print(forces)
total_energy = atoms.get_total_energy()
print(total_energy)
# Create SOAP output for the system
soap_h2 = soap.create(atoms, positions=[CENTER])
print(soap_h2.shape)

#############################
# trajectory of h2 molecule
traj = []
traj_forces = []
traj_energies = []
r = np.arange(2.5, 5.0, 0.1) # bond lengths of h2
for d in r:
	#print(d)
	a = ase.Atoms('HH', positions = [[-0.5 * d, 0, 0], [0.5 * d, 0, 0]])
	a.set_calculator(LennardJones(epsilon = 10.0 , sigma = 2.9))
	traj_forces.append(a.get_forces())
	traj.append(a)
	traj_energies.append(a.get_total_energy())
	
print('Trajectory total energies')
print(traj_energies)

import matplotlib.pyplot as plt
print('h2 bond lengths')
print(r)
y = traj_energies
#fig, ax = plt.figure()
fig, ax = plt.subplots()
#ax = fig.add_subplot(2, 1, 1)
line, = ax.plot(r, y, color='blue', lw=2)
#ax.set_yscale('log')
plt.show()


# Create SOAP output for the system
soap_traj = soap.create(traj, positions=[CENTER] * len(traj))

print(soap_traj)
print(soap_traj.shape)