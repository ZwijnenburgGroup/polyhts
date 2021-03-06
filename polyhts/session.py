#!/usr/bin/env python

import rdkit, rdkit.Chem as rdkit
import stk
from joblib import Parallel, delayed

import subprocess as sp
import string
import time
import random
import os, errno, shutil, itertools, operator

from .utilities import *


class Session:

    """
    Builds a session, in which polymer structures may be constructed,
    optimized, have their properties calculated (IP, EA & S0->S1 transiton).
    Lists of monomers may be provided, co-polymer combinations of which
    may be automatically screened.

    parameters
    ----------

    session_name : :class:`str`
        Name of the session. Essentially the name of the directory into which
        all results will be placed.

    length_repeat : :class:`int`
        Number of monomers in repeat unit.

    n_repeat : :class:`int`
        Number of repeat units that will be used to build a polymer chain.

    n_confs : :class:`int`
        Number of conformers to embed within conformer search.

    solvent : :class:`str` (default = ``None``)
        Solvent to be applied within calculations. Solvent effects are
        applied using an implicit model.

    Methods
    -------

    calc_polymer_properties :
        Calculate polymer properties for a specified co-polymers

    screen :
        Calculate polymer properties for all combinations of a list of
        monomers, represented by SMILES strings. The list of SMILES
        should be provided via an input file (see method docs).

    returns
    -------

    str : :class:`str`
        A description of the current session.

    """

    def __init__(self, session_name, length_repeat, n_repeat, n_confs, solvent=None):
        self.session_name = session_name
        self.length_repeat = length_repeat
        self.n_repeat = n_repeat
        self.n_confs = n_confs

        try:
            os.makedirs(self.session_name)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise

        self.solvent_info = []
        if solvent is not None:
            if solvent not in valid_solvents:
                raise Exception('Invalid solvent choice. Valid solvents:',
                [i for i in valid_solvents])
            else:
                self.solvent_info = ['-gbsa', solvent]

        with open('temp', 'w') as f:
            f.write('')


    def calc_polymer_properties(self, smiles, name):

        """
        Calculate properties for a specified co-polymer composition, specified
        using a pair of smiles strings.
        Note that all other properties (number of repeat units, solvent,
        number of conformers to search) are inferred from the Session class.

        Parameters
        ----------

        smiles : :class:`list`
            smiles used to construct polymer repeat unit

        """

        monomers_dict = {}
        permutation = []
        for i in range(len(smiles)):
            monomers_dict[string.ascii_uppercase[i]] = smiles[i]
            permutation.append(string.ascii_uppercase[i])

        with cd(self.session_name):
            try:
                polymer, repeat = self.generate_polymer(permutation, monomers_dict, name)
                conf, E = self.conformer_search(polymer)
                E_xtb, E_solv = self.xtb_opt(polymer)
                vip, vea = self.xtb_calc_potentials(polymer)
                gap, f = self.stda_calc_excitation(polymer)

                print_formatted_properties(polymer.name, vip, vea, gap, f, E_solv)
                remove_junk()

            except Exception as e:
                print(e)
                remove_junk()


    def screen(self, monomers_file, nprocs=1, random_select=False):

        """
        Parameters
        ----------

        monomers_file : :class:`str`
            A file containing a list of monomer units to be screened.
            All binary co-polymer compositions are screened.

        nprocs : :class:`int` (default = ``1``)
            Number of cores to be used when screening. If a number greater
            than 1 is chosen, polymers are screened in parallel, one polymer
            composition per core.
            Note that, since results are printed as they
            are avalable, polymers compositions in the output file will
            not be ordered. Instead, once the entire screening process is
            complete, the output is ordered and re-writted.

        random_select : :class:`int` (default = ``False``)
            Randomly select co-polymer combinations to screen. Number of randomly
            chosen compositions is given by an integer value. This may be useful
            if one requires randomly sampled compositions from the overall
            co-polymer composition space.

        Returns
        -------
        None : :class:`NoneType`

        'screening-output' : :class:`file`
            Output file containing properties of screened polymer compositions
        """

        with open(monomers_file) as f:
            monomers = [line.split() for line in f]

        monomers_dict = {}
        for id, smiles in monomers:
            monomers_dict[id] = smiles

        with open(self.session_name+'/'+'screening-output', 'w') as output:
            output.write(output_header)

        results = Parallel(n_jobs=nprocs)(delayed(self.screening_protocol)
        (permutation, monomers_dict) for permutation in self.get_polymer_compositions(monomers_dict, random_select))


    def screening_protocol(self, permutation, monomers_dict):

        time.sleep(1)
        with open('temp') as f:
            screened = [tuple(i.split()) for i in f]

        if permutation not in screened and permutation[::-1] not in screened:
            with open('temp', 'a+') as f:
                f.write(' '.join(list(permutation))+'\n')

            name = '-'.join(id for id in permutation)

            try:
                os.makedirs(self.session_name+'/'+name)
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise

            with cd(self.session_name+'/'+name):
                try:
                    polymer, repeat = self.generate_polymer(permutation, monomers_dict, name)
                    conf, E = self.conformer_search(polymer)
                    E_xtb, E_solv = self.xtb_opt(polymer)
                    vip, vea = self.xtb_calc_potentials(polymer)
                    gap, f = self.stda_calc_excitation(polymer)
                    property_log(repeat, vip, vea, gap, f, E_solv)
                    remove_junk()

                except Exception as e:
                    error_log(permutation, monomers_dict, e)
                    remove_junk()


    def get_polymer_compositions(self, monomers_dict, random_select):

        monomers = list([mon for mon in monomers_dict])

        if not random_select:
            product = itertools.product(monomers, repeat=self.length_repeat)
            for item in product:
                yield item

        else:
            product = itertools.product(monomers, repeat=self.length_repeat)
            len_product = 0
            for i in product:
                len_product += 1

            counter = 0
            while counter < random_select:
                choice = random.randint(0, len_product)
                for index, item in enumerate(itertools.product(monomers, repeat=self.length_repeat)):
                    if index == choice:
                        yield item
                        counter += 1


    def generate_polymer(self, permutation, monomers_dict, name):

        sequence = string.ascii_uppercase[:len(permutation)]
        isomers = [0 for i in permutation]

        structunits = []
        for id in permutation:
            smiles = rdkit.MolToSmiles(rdkit.MolFromSmiles(monomers_dict[id]), canonical=True)
            mol = rdkit.AddHs(rdkit.MolFromSmiles(smiles))
            rdkit.AllChem.EmbedMolecule(mol, rdkit.AllChem.ETKDG())
            structunits.append(stk.StructUnit2.rdkit_init(mol, "bromine"))

        repeat = stk.Polymer(structunits, stk.Linear(sequence, isomers, n=1), name=name)
        polymer = stk.Polymer(structunits, stk.Linear(sequence, isomers, n=self.n_repeat), name=name)
        rdkit.MolToMolFile(polymer.mol, 'test.mol')

        return polymer, repeat


    def conformer_search(self, polymer):

        mol, name = polymer.mol, polymer.name
        confs = rdkit.AllChem.EmbedMultipleConfs(mol, self.n_confs, rdkit.AllChem.ETKDG())
        rdkit.SanitizeMol(mol)

        lowest_energy = 10**10
        for conf in confs:
            ff = rdkit.AllChem.MMFFGetMoleculeForceField(mol, rdkit.AllChem.MMFFGetMoleculeProperties(mol), confId=conf)
            ff.Initialize()
            energy = ff.CalcEnergy()

            if energy < lowest_energy:
                lowest_energy = energy
                lowest_conf = conf

        rdkit.MolToMolFile(mol, name+'.mol', confId=lowest_conf)

        return lowest_conf, lowest_energy


    def xtb_opt(self, polymer):

        name = polymer.name
        molfile = '{}.mol'.format(name)
        xyzfile = '{}.xyz'.format(name)
        sp.call(['babel', molfile, xyzfile])

        calc_params = ['xtb', xyzfile, '-opt'] + self.solvent_info
        output = run_calc(calc_params)

        with open('opt-calc.out', 'w') as f:
            f.write(output)

        if len(self.solvent_info) > 0:
            E_xtb  = output[-900:-100].split()[27]
            E_solv = str(float(output[-900:-100].split()[18])*27.2114)[:6]
        else:
            E_xtb  = output[-900:-100].split()[29]
            E_solv = None

        shutil.copy('xtbopt.xyz', '{}-opt.xyz'.format(name))

        return E_xtb, E_solv


    def xtb_calc_potentials(self, polymer):

        name = polymer.name
        xyzfile = '{}-opt.xyz'.format(name)

        # calculate and extract IP
        calc_params = ['xtb', xyzfile, '-vip'] + self.solvent_info
        output = run_calc(calc_params)
        vip = output[output.find('delta SCC IP'):].split()[4]

        # calculate and extract EA
        calc_params = ['xtb', xyzfile, '-vea'] + self.solvent_info
        output = run_calc(calc_params)
        vea = output[output.find('delta SCC EA'):].split()[4]

        return vip, vea


    def stda_calc_excitation(self, polymer):

        name = polymer.name
        xyzfile = '{}-opt.xyz'.format(name)

        # calculate xtb wavefunction
        calc_params = ['xtb', xyzfile] + self.solvent_info
        run_calc(calc_params)

        # calculate excitations, extract S0 -> S1, extract f
        calc_params = ['stda', '-xtb', '-e', '8']
        output = run_calc(calc_params)

        with open('stda-calc.out', 'w') as f:
            f.write(output)

        gap = output[output.find('excitation energies'):].split()[13]
        f = output[output.find('excitation energies'):].split()[15]

        return gap, f


    def output_sort(self):

        with open(self.session_name+'/screening-output') as f:
            lines = [line.split() for line in f]
            header = lines[0]
            content = lines[1:]
            content.sort(key = operator.itemgetter(0, 1))

        with open(self.session_name+'/screening-output', 'w') as f:
            f.write('{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\n'.format(*header))
            for line in content:
                f.write('{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\n'.format(*line))


    def __str__(self):

        string = 'Session name: ' + self.session_name + '\n'
        string += 'Length of repeat unit: ' + str(self.length_repeat) + '\n'
        string += 'Num. repeat units: ' + str(self.n_repeat) + '\n'
        string += 'Num. conformers: ' + str(self.n_confs) + '\n'
        if len(self.solvent_info) > 0:
            string += 'Solvent: ' + self.solvent_info[1] + '\n'

        return string
