require 'rails_helper'

RSpec.describe Freelancer, type: :model do
  describe 'validations' do
    it 'is valid with a name, email and hourly_rate' do
      freelancer = described_class.new(
        name: 'Jane Smith',
        email: 'jane@example.com',
        hourly_rate: 75.00
      )
      expect(freelancer).to be_valid
    end

    it 'is invalid without a name' do
      freelancer = described_class.new(name: nil)
      expect(freelancer).not_to be_valid
      expect(freelancer.errors[:name]).to include("can't be blank")
    end

    it 'is invalid without an email' do
      freelancer = described_class.new(email: nil)
      expect(freelancer).not_to be_valid
      expect(freelancer.errors[:email]).to include("can't be blank")
    end

    it 'is invalid with a duplicate email' do
      described_class.create!(name: 'Jane', email: 'jane@example.com', hourly_rate: 75.00)
      duplicate = described_class.new(name: 'Other Jane', email: 'jane@example.com', hourly_rate: 80.00)
      expect(duplicate).not_to be_valid
      expect(duplicate.errors[:email]).to include('has already been taken')
    end

    it 'is invalid with a negative hourly rate' do
      freelancer = described_class.new(hourly_rate: -10)
      expect(freelancer).not_to be_valid
      expect(freelancer.errors[:hourly_rate]).to include('must be greater than or equal to 0')
    end
  end
end
