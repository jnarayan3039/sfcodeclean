from django.conf import settings
from django.utils import timezone

from .models import Job, ApexClass, ApexPageComponent

from bs4 import BeautifulSoup

import uuid
import requests
import time
import json

REST_URL = '/services/data/%d.0/' % settings.SALESFORCE_API_VERSION
TOOLING_URL = '%s/tooling/' % REST_URL


class ScanJob(object):
    """
    Holds all the logic for processing a job
    """

    job = None
    headers = {}
    tooling_url = None

    def __init__(self, job):
        """Init variables for the class"""
        self.job = job
        self.headers = {
            'Authorization': 'Bearer %s' % self.job.access_token,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        self.tooling_url = '%s%s' % (self.job.instance_url, settings.SALESFORCE_TOOLING_URL)


    def get_all_records(self, object_name):
        """
        Queries for all records specified by the object_name
        """
        records = []

        url = '%squery/?q=SELECT+Id,Name,%s+FROM+%s+WHERE+NamespacePrefix=NULL' % (
            self.tooling_url, ('Body' if object_name == 'ApexClass' else 'Markup,ControllerKey,ControllerType'), object_name
        )

        result = requests.get(url, headers=self.headers)
        records.extend(result.json().get('records'))

        # If there are more classes, we need to keep calling for more.
        while 'nextRecordsUrl' in result.json():
            result = requests.get(self.job.instance_url + result.json().get('nextRecordsUrl'), headers=self.headers)
            records.extend(result.json().get('records'))
        return records

    def get_extensions_from_body(self, body):
        """
        Retrieve the extensions for a VisualForce page body
        """

        extensions_list = []

        # Load a soup object for the VF page
        # BeautifulSoup is an HTML parser
        # VF is pretty close to HTML, so going to leverage
        # that library to find any controllers or extensions for the VF
        soup = BeautifulSoup(body, 'html.parser')

        # Load the apex:page attribute
        page_attribute = soup.findAll({'apex:page'})

        if page_attribute:

            page_attribute = page_attribute[0]

            # Load the extensions from the attribute
            extensions = page_attribute.get('extensions','').strip()

            if extensions:
                # There could be multiple extensions (seperated by comma), so process
                # them individually
                for extension in extensions.split(','):
                    # Trim any whitespace and add to the list to reutrn
                    extensions_list.append(extension.strip())
                
        return extensions_list


    def get_visualforce(self, object_name):
        """
        Generic method for loading all ApexPage and ApexComponent components from the Org
        """

        # Load all VF and Components
        for visualforce in self.get_all_records(object_name):
            new_vf = ApexPageComponent()
            new_vf.job = self.job
            new_vf.sf_id = visualforce.get('Id')
            new_vf.name = visualforce.get('Name')
            new_vf.body = visualforce.get('Markup')

            controllers = []

            # If ControlerType == true, this covers ApexComponent controllers
            # And VF pages where the controller isn't a standard controller
            if visualforce.get('ControllerType') == '2' and visualforce.get('ControllerKey') and 'NullController' not in visualforce.get('ControllerKey'):
                controllers.append(visualforce.get('ControllerKey'))

            # For ApexPages, we also need to check the extensions to see if there's any values to add
            if object_name == 'ApexPage':
                controllers.extend(self.get_extensions_from_body(new_vf.body))

            # Add the controllers to the text field
            if controllers:
                new_vf.controller = ','.join(controllers)

            new_vf.type = 'Page' if object_name == 'ApexPage' else 'Component'
            new_vf.save()


    def get_metadata_container_id(self):
        """
        Creates the MetadataContainer used to hold the working copies of ApexClassMember
        https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/tooling_api_objects_metadatacontainer.htm?search_text=MetadataContainer
        """

        url = '%ssobjects/MetadataContainer' % (self.tooling_url)
        result = requests.post(url, headers=self.headers, json={'Name': str(uuid.uuid4())[:32]})
        return result.json().get('id')


    def create_class_member(self, metadata_container_id, apex_class):
        """
        Create a class member for the Apex Class
        """

        url = '%ssobjects/ApexClassMember' % (self.tooling_url)
        data = {
            'Body': apex_class.body,
            'ContentEntityId': apex_class.class_id,
            'MetadataContainerId': metadata_container_id
        }
        result = requests.post(url, headers=self.headers, json=data)
        return result.json().get('id')


    def create_container_request(self, metadata_container_id):
        """
        Runs the Async request to compile the code
        """
        url = '%ssobjects/ContainerAsyncRequest' % (self.tooling_url)
        data = {
            'IsCheckOnly': True,
            'MetadataContainerId': metadata_container_id
        }
        # This returns an ID, and must be re-queried until it's finishd
        result = requests.post(url, headers=self.headers, json=data)
        return result.json().get('id')


    def get_compile_status(self, compile_id):
        """
        Check the status of the compile job
        """
        url = '%ssobjects/ContainerAsyncRequest/%s' % (self.tooling_url, compile_id)
        result = requests.get(url, headers=self.headers)
        return result.json()


    def get_symbol_table_for_class(self, class_member_id):
        """
        Retrieves the symbol table for a class
        """
        url = '%ssobjects/ApexClassMember/%s' % (self.tooling_url, class_member_id)
        result = requests.get(url, headers=self.headers)
        return json.dumps(result.json().get('SymbolTable'))


    def get_class_to_vf_usage_dict(self):
        """
        First things first, we're going to go through all the Apex Pages and Components
        And build a dictionary of each Apex Class and the VisualForce it's used it
        That way, when we go through the classes later
        We can go through the methods and properties later on
        Eg.
        {
            'AccountController': [
                'Account.page',
                'Account.component'
            ],
            'AccountExtensionController': [
                'Account.page'
            ]
        }
        """
        apex_to_vf = {}

        for visualforce in self.job.visualforce():

            # If there was a controller found, add it to the dictionary
            if visualforce.controller:

                # There could be multiple controllers, so iterate through the list
                for controller in visualforce.controller.split(','):

                    # Add the controller to the dict
                    if controller in apex_to_vf:
                        apex_to_vf[controller].append(visualforce)
                    else:
                        apex_to_vf[controller] = [visualforce]

        return apex_to_vf

                   

    def process_external_references(self):
        """
        For each Apex Class, now process all the external references
        Basically, the SymbolTable returns all the classes and methods that "this" class references
        But we want to flip that around and for each class, work out what external classess call "this" class
        So, what we do is go through all the classes and methods that a class calls, and then built a dict and map this
        back to the each class, and store it on that class to display in the UI later
        """

        # First things first, we're going to go through all the Apex Pages and Components
        # And build a dictionary of each Apex Class and the VisualForce it's used it
        # That way, when we go through the classes later
        # We can go through the methods and properties later on
        apex_to_vf = self.get_class_to_vf_usage_dict()

        references_dict = {}
        
        # Iterate over the classes
        for apex_class in self.job.classes():

            if apex_class.symbol_table_json:

                # Load the JSON SymbolTable into a Python dict. 
                # We need to traverse this to build a dict of all the external references, and 
                # map back to the class
                symbol_table = json.loads(apex_class.symbol_table_json)

                # Create an empty reference object to populate all the references to
                reference_object = {
                    'visualforce': [],
                    'classes': {},
                    'methods': {},
                    'variables': {},
                    'properties': {},
                }

                # Add any VF pages as class references
                if apex_class.name in apex_to_vf:

                    # Add the VF references to the class
                    for visualforce in apex_to_vf.get(apex_class.name):
                        # Add the list of references to the class
                        reference_object['visualforce'].append(self.get_vf_name(visualforce))

                    # Let's see what methods are used in VisualForce
                    if symbol_table and symbol_table.get('methods'):

                        for method in symbol_table.get('methods'):

                            # Get the VF method name
                            method_vf_name = '{!' + method.get('name') + '}'

                            for visualforce in apex_to_vf.get(apex_class.name):

                                vf_name = visualforce.name + ' (' + visualforce.type + ')'

                                # Determine if the method name is found in the VF page
                                if method_vf_name in visualforce.body:

                                    # Need to determine if a key for the method already exists
                                    if method.get('name') in reference_object.get('methods'):
                                        method_references = reference_object.get('methods').get(method.get('name'))
                                    else:
                                        method_references = {}

                                    # Add the VisualForce page as a method reference
                                    if self.get_vf_name(visualforce) not in method_references:
                                        method_references[self.get_vf_name(visualforce)] = []

                                    reference_object['methods'][method.get('name')] = method_references

                    # And now do the properties
                    if symbol_table and symbol_table.get('properties'):
                        
                        for apex_property in symbol_table.get('properties'):

                            # Get the VF method name
                            property_vf_name = '{!' + apex_property.get('name') + '}'

                            for visualforce in apex_to_vf.get(apex_class.name):

                                # Determine if the method name is found in the VF page
                                if property_vf_name in visualforce.body:

                                    # Need to determine if a key for the method already exists
                                    if apex_property.get('name') in reference_object.get('properties'):
                                        property_references = reference_object.get('properties').get(apex_property.get('name'))
                                    else:
                                        property_references = []

                                    # Add the VisualForce page as a method reference
                                    if self.get_vf_name(visualforce) not in property_references:
                                        property_references.append(self.get_vf_name(visualforce))

                                    reference_object['properties'][apex_property.get('name')] = property_references


                    # Push back into the Dict
                    references_dict[apex_class.name] = reference_object

                # Now, load any external references for the class.
                # This is all Apex that this class CALLS OUT to, not what references it
                if symbol_table and symbol_table.get('externalReferences'):

                    # Iterate over each external reference for the class
                    for external_reference in symbol_table.get('externalReferences'):

                        # We don't want to include anything with a namespace
                        if not external_reference.get('namespace'):

                            # If the reference already exists, take the existing dict
                            if external_reference.get('name') in references_dict:
                                reference_object = references_dict.get(external_reference.get('name'))

                            # Now add in the line and method references
                            # These are any references to a class that isn't a method or property
                            # Eg. Calling the class or constructor: MyClass myClass = new MyClass();
                            for line in external_reference.get('references', []):

                                references = [self.get_line_description(line)]

                                # If the class already exists, add the new reference as a child
                                if apex_class.name in reference_object['classes']:
                                    references = reference_object['classes'][apex_class.name]
                                    references.append(self.get_line_description(line))

                                # Add the list of references to the class
                                reference_object['classes'][apex_class.name] = references

                            # Now iterate over all the methods to determine the references
                            # For each method
                            for method in external_reference.get('methods', []):

                                method_references = {}

                                # Need to determine if a key for the method already exists
                                if method.get('name') in reference_object.get('methods'):
                                    method_references = reference_object.get('methods').get(method.get('name'))

                                lines = []
                                if apex_class.name in method_references:
                                    lines = method_references.get(apex_class.name)

                                for line in method.get('references', []):
                                    lines.append(self.get_line_description(line))

                                method_references[apex_class.name] = lines

                                # Add back to the object map
                                reference_object['methods'][method.get('name')] = method_references


                            # Now process the variables
                            for variable in external_reference.get('variables', []):

                                print(variable)

                                variable_references = {}
                                
                                # Need to determine if a key for the method already exists
                                if variable.get('name') in reference_object.get('variables'):
                                    variable_references = reference_object.get('variables').get(variable.get('name'))
                               
                                lines = []
                                if apex_class.name in variable_references:
                                    lines = variable_references.get(apex_class.name)

                                for line in variable.get('references', []):
                                    lines.append(self.get_line_description(line))

                                variable_references[apex_class.name] = lines

                                print variable_references

                                # Add back to the object map
                                reference_object['variables'][variable.get('name')] = variable_references


                        # Push back into the Dict
                        references_dict[external_reference.get('name')] = reference_object

        # Now, map back to the ApexClasses
        for apex_class in self.job.classes():

            # If the Apex Class is referenced external, dump the references
            if apex_class.name in references_dict:
                apex_class.is_referenced_externally = True
                apex_class.referenced_by_json = json.dumps(references_dict.get(apex_class.name))

            # Else dump in an empty array
            else:
                apex_class.referenced_by_json = json.dumps({
                    'visualforce': [],
                    'classes': {},
                    'methods': {},
                    'variables': {},
                    'properties': {},
                })

            # Save the class
            apex_class.save()


    def get_vf_name(self, visualforce):
        return visualforce.name + ' (' + visualforce.type + ')'

    def get_lines_array(self, apex_class_name, lines):
        """
        Gets the array of line references for a class, method or variable
        """
        lines_display = []
        for line in lines:
            if self.get_line_description(apex_class_name, line) not in lines_display:
                lines_display.append(self.get_line_description(apex_class_name, line))
        return lines_display


    def get_line_description(self, line):
        """
        Build the line description for each reference
        """
        return 'Line %d Column %d' % (line.get('line'), line.get('column'))


    def scan_org(self):
        """
        Execute all the logic to scan the Org
        """

        # Delete any existing classes
        self.job.classes().delete()
        self.job.visualforce().delete()

        # Create the metadata container
        metadata_container_id = self.get_metadata_container_id()

        classes = []

        # Query for and get all classes
        for apex_class in self.get_all_records('ApexClass'):

            # Create the new class
            new_class = ApexClass()
            new_class.job = self.job
            new_class.class_id = apex_class.get('Id')
            new_class.name = apex_class.get('Name')
            new_class.body = apex_class.get('Body')
            new_class.save()

            # Create a ApexClassMember for the class
            new_class.class_member_id = self.create_class_member(metadata_container_id, new_class)
            new_class.save()

            classes.append(new_class)


        # Load all the Apex Pages and Apex Components as well
        self.get_visualforce('ApexPage')
        self.get_visualforce('ApexComponent')

        # Now we have created a ApexClassMember for each class, we need to "compile" all the classes
        # This runs to build the symbol table
        compile_request_id = self.create_container_request(metadata_container_id)

        # Continue to check for the compile results
        compile_complete = False
        compile_status = None
        while not compile_complete:
            time.sleep(3)
            compile_status = self.get_compile_status(compile_request_id)
            compile_complete = compile_status.get('State') in ['Invalidated','Completed','Failed','Error','Aborted']
            
        if compile_status.get('State') != 'Completed':

            self.job.status = 'Error'

            # Build an array of errors
            errors = []

            # Add in the master error
            if compile_status.get('ErrorMsg'):
                errors.append(compile_status.get('ErrorMsg'))

            # Build a list of errors
            for component in compile_status.get('DeployDetails',{}).get('allComponentMessages'):
                if not component.get('success'):
                    errors.append(component.get('fullName') + ': ' + component.get('problem'))

            self.job.error = 'Code compilation error:\n- %s' % ('\n- '.join(errors))
            self.job.save()
            return

        # Once complete, we can now pull the SymbolTable for each ApexClass
        for apex_class in classes:
            apex_class.symbol_table_json = self.get_symbol_table_for_class(apex_class.class_member_id)
            apex_class.save()


        # Re-query for the job, to load all new child references
        self.job = Job.objects.get(pk=self.job.pk)

        # For each Apex Class, now process all the external references
        # Basically, the SymbolTable returns all the classes and methods that "this" class references
        # But we want to flip that around and for each class, work out what external classess call "this" class
        self.process_external_references()

        self.job.finished_date = timezone.now()
        self.job.status = 'Finished'
        self.job.save()
            

